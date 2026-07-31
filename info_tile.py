"""One FC HUD info tile: an opaque, override-redirect Tk Toplevel with a caption
strip (title + close glyph) over a plain content body.

This is the FCPreview tile's ERGONOMICS without any of its expensive parts. It
hosts INFORMATION, not video, so there is no DWM thumbnail, no letterboxing, no
character identity, no activation ladder and no zoom. What it does keep, because
those are the behaviours the owner asked to reuse:

- Toplevel, overrideredirect(True), always -topmost, ex-styles
  WS_EX_TOOLWINDOW|WS_EX_NOACTIVATE set / WS_EX_APPWINDOW cleared on the GA_ROOT
  hwnd, so a tile is out of Alt-Tab and a click on it never steals the game's
  focus.
- Placement ONLY via win32.set_window_pos(GA_ROOT, x, y, w, h) in PHYSICAL px --
  never Tk geometry, which is logical px under PMv2 and would misplace the tile
  on a scaled monitor.
- Mouse model: LEFT-drag on the caption strip moves; RIGHT-drag anywhere moves;
  hovering a corner arms a resize whose OPPOSITE corner stays anchored. Every
  drag is a no-op while lock_layout is on. The strip's close glyph reports
  on_close(tile_key) and never destroys anything -- the controller owns tile
  lifecycle.

DESIGN NOTES that are load-bearing (docs/agents/map/preview.md):

- All geometry MATH is preview_layout's. ``MIN_W`` is an ALIAS of
  ``preview_layout.MIN_TILE_W`` (identity -- never a re-typed literal), because
  a size floor with two owners is exactly how a preview tile once got driven to
  0 px wide by a path that did not know the constant existed. ``MIN_H`` is this
  module's own: preview's ``MIN_TILE_BODY_H`` is a DWM *body* height (its strip
  is extra) and does not describe a full info-tile window.
- This module's rects are (x, y, w, h) with h the FULL window height, strip
  INCLUDED. preview_tile's are (x, y, w, body_h) and it converts to full height
  before snapping. A caller merging preview rects into ``neighbor_provider``
  therefore has to add preview's STRIP_H itself -- these tiles snap against
  VISIBLE edges.
- No per-tick retop: ``retop`` is called inside ``show()`` and nowhere else
  (the measured DWM-thrash / in-game FPS lesson from the on-video overlay saga).
- Guarded setters latch their last-applied mirror ONLY after a successful Tk
  write, so a TclError can never poison a guard into skipping a real update.
- ``set_alpha`` compares before it writes, and re-asserts the ex-style bits
  after a REAL change: Tk's ``wm attributes -alpha`` rewrites the whole
  GWL_EXSTYLE word on the opaque<->layered transition, wiping WS_EX_TOOLWINDOW.
- Fonts are plain (family, size, weight) tuples, never ``tkfont.Font`` objects:
  a Font belongs to the interpreter that made it, so module-level ones break.

All Win32 lives behind an injectable backend (default ``_real_info_win32()``);
tests inject a recorder fake. ``preview_tile`` is neither imported nor modified.
Tk-thread only touches this object (house rule #1).
"""
from __future__ import annotations

import tkinter as tk

import preview_layout
import ui_theme

STRIP_H = 20              # caption-strip height in px (this module's own)
_MOVE_JITTER = 4          # a left press/release within this many px is a click
_CORNER_ZONE = 12         # px hot-zone at each corner that arms a resize
# Minimum FULL window height (strip + body). preview_layout owns the WIDTH floor
# for both tile families; the height floor is ours because preview's is a body
# height for a letterboxed video. 90 px = the 20 px strip plus ~70 px of body,
# enough for a title line and ~4 rows of 9 pt text, and >= 2x clamp_visible's
# 40 px "usably visible" threshold so a floored tile stays grabbable.
_MIN_TILE_H = 90

# Tk diagonal-resize cursor names (Windows): NW/SE share one, NE/SW the other.
_CORNER_CURSOR = {"nw": "size_nw_se", "se": "size_nw_se",
                  "ne": "size_ne_sw", "sw": "size_ne_sw"}

# Strip close glyph. GUI text only -- never passed to log.*, where a non-cp1252
# glyph raises inside logging's StreamHandler on this box (map/facts.md).
CLOSE_GLYPH = "✕"


def _as_int(value, default=0):
    """`value` as an int, or `default` for junk (None, "", NaN, a string).
    Coordinates arrive from config and from callers' saved layouts, so a
    hand-edited value must degrade rather than raise."""
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


class _RealInfoWin32:  # pragma: no cover -- ctypes, mirrors preview_tile's shim
    """Real ctypes wrapper for the info tile's Alt-Tab-exclusion / no-activate
    styling and physical-pixel placement.

    Deliberately this module's OWN class rather than an import of
    ``preview_tile._RealTileWin32``: reaching across for a private name would
    couple the two window families for four short methods and would put info
    tiles inside preview's regression surface. The surface mirrors preview's
    (get_root_hwnd / exclude_from_alt_tab / set_window_pos / retop) so one fake
    shape serves both suites."""

    GWL_EXSTYLE = -20
    WS_EX_TOOLWINDOW = 0x00000080
    WS_EX_APPWINDOW = 0x00040000
    WS_EX_NOACTIVATE = 0x08000000
    HWND_TOPMOST = -1
    SWP_NOMOVE = 0x0002
    SWP_NOSIZE = 0x0001
    SWP_NOZORDER = 0x0004
    SWP_NOACTIVATE = 0x0010
    SWP_FRAMECHANGED = 0x0020
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
        GetAncestor(child, GA_ROOT) first, GetParent fallback, child last."""
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

    def exclude_from_alt_tab(self, hwnd: int) -> None:
        """Keep this OWN top-level window out of the Alt-Tab switcher and off
        the taskbar, and mark it no-activate: set WS_EX_TOOLWINDOW +
        WS_EX_NOACTIVATE, clear WS_EX_APPWINDOW.

        The ``want == ex`` compare-before-set gate is LOAD-BEARING, not an
        optimization: without it every re-assert would issue a
        SetWindowPos(FRAMECHANGED) and flicker the window."""
        ex = int(self._get(hwnd, self.GWL_EXSTYLE))
        want = ((ex | self.WS_EX_TOOLWINDOW | self.WS_EX_NOACTIVATE)
                & ~self.WS_EX_APPWINDOW)
        if want == ex:
            return
        self._set(hwnd, self.GWL_EXSTYLE, want)
        self._user32.SetWindowPos(
            hwnd, 0, 0, 0, 0, 0,
            self.SWP_NOMOVE | self.SWP_NOSIZE | self.SWP_NOZORDER
            | self.SWP_NOACTIVATE | self.SWP_FRAMECHANGED)

    def set_window_pos(self, hwnd: int, x: int, y: int, w: int, h: int) -> None:
        self._user32.SetWindowPos(hwnd, self.HWND_TOPMOST, x, y, w, h,
                                  self.SWP_NOACTIVATE)

    def retop(self, hwnd: int) -> None:
        self._user32.SetWindowPos(
            hwnd, self.HWND_TOPMOST, 0, 0, 0, 0,
            self.SWP_NOMOVE | self.SWP_NOSIZE | self.SWP_NOACTIVATE)


_REAL_INFO_WIN32 = None


def _real_info_win32():  # pragma: no cover
    global _REAL_INFO_WIN32
    if _REAL_INFO_WIN32 is None:
        _REAL_INFO_WIN32 = _RealInfoWin32()
    return _REAL_INFO_WIN32


class _MoveState:
    """One drag gesture's anchor. Two instances exist per tile (the strip's
    left-drag and the body's right-drag) so a stray press on one path can never
    corrupt the other's anchor."""

    __slots__ = ("root", "pos", "moving")

    def __init__(self):
        self.root = None       # (x_root, y_root) at press, None when idle
        self.pos = (0, 0)      # tile top-left at press (physical px)
        self.moving = False    # True once the gesture passed _MOVE_JITTER


class InfoTileWindow:
    """One FC HUD info tile. Chrome only -- the caller fills ``body``.
    Tk-thread only."""

    # ALIAS, never a re-typed literal: preview_layout is the SINGLE owner of the
    # width floor for both tile families (identity-asserted by the tests).
    MIN_W = preview_layout.MIN_TILE_W
    MIN_H = _MIN_TILE_H

    def __init__(self, root, tile_key: str, title: str, palette: dict,
                 win32=None, on_move_end=None, on_resize_end=None,
                 on_close=None):
        self._win32 = win32 or _real_info_win32()
        self._key = tile_key
        self._palette = palette or {}
        # Callbacks all carry the FULL rect so the controller can persist
        # layouts[key] = [x, y, w, h] from either one without a second lookup.
        self._on_move_end = on_move_end or (lambda k, x, y, w, h: None)
        self._on_resize_end = on_resize_end or (lambda k, x, y, w, h: None)
        self._on_close = on_close or (lambda k: None)

        # geometry (physical px, virtual-screen space). Seeded at the floors so
        # a show() before the first place() still yields a visible, grabbable
        # window and corner detection has real dimensions to work with.
        self._pos = (0, 0)
        self._w = self.MIN_W
        self._h = self.MIN_H
        self._visible = False          # mirrors mapped/withdrawn state
        self._lock_layout = False

        # snapping (pushed live by the controller; see configure_snap)
        self._snap_enabled = False
        self._snap_threshold = preview_layout.SNAP_THRESHOLD_PX
        self._neighbor_provider = None
        self._screens_provider = None

        # drag state
        self._strip_move = _MoveState()
        self._b3_move = _MoveState()
        self._corner = None             # armed corner under the pointer
        self._corner_resizing = False
        self._corner_anchor = None      # (ax, ay) opposite corner, stays fixed
        self._corner_press_root = None
        self._corner_press_size = None
        self._corner_press_pos = None
        self._close_pressed = False

        # LAST-APPLIED mirrors for the guarded setters. Each is updated only
        # AFTER its Tk write succeeds, so a TclError leaves the guard hungry and
        # the next call retries instead of believing a failed write landed.
        self._alpha = 1.0
        self._title_applied = ""

        bg_panel = self._palette.get("BG_PANEL", ui_theme.BG_PANEL)
        bg_dark = self._palette.get("BG_DARK", ui_theme.BG_DARK)
        fg_text = self._palette.get("FG_TEXT", ui_theme.FG_TEXT)
        fg_dim = self._palette.get("FG_DIM", ui_theme.FG_DIM)
        self._fg_dim = fg_dim
        self._fg_close_hot = self._palette.get("FG_RED", ui_theme.FG_RED)

        self.top = tk.Toplevel(root)
        self.top.overrideredirect(True)
        self.top.configure(bg=bg_dark)
        # Withdraw BEFORE anything pumps the event loop, so the window is never
        # briefly mapped (and never briefly in Alt-Tab) during construction.
        self.top.withdraw()
        try:
            self.top.attributes("-topmost", True)
        except tk.TclError:
            pass

        # -- caption strip: title left, close glyph right --------------------
        self._strip = tk.Frame(self.top, bg=bg_panel, height=STRIP_H)
        self._strip.pack(fill="x", side="top")
        self._strip.pack_propagate(False)

        # packed first so the expanding title takes only what is left over
        self._close_lbl = tk.Label(self._strip, text=CLOSE_GLYPH, bg=bg_panel,
                                   fg=fg_dim, font=("Consolas", 9, "bold"),
                                   cursor="hand2")
        self._close_lbl.pack(side="right", padx=(2, 5))

        title_text = "" if title is None else str(title)
        self._title_lbl = tk.Label(self._strip, text=title_text, bg=bg_panel,
                                   fg=fg_text, anchor="w",
                                   font=("Consolas", 9, "bold"))
        self._title_lbl.pack(side="left", fill="x", expand=True, padx=(5, 2))
        self._title_applied = title_text

        # -- body: the renderers' parent -------------------------------------
        self.body = tk.Frame(self.top, bg=bg_dark)
        self.body.pack(fill="both", expand=True, side="top")

        self.top.update_idletasks()
        self._hwnd = self._resolve_hwnd()
        self._assert_ex_style()        # before the first map
        self._bind_mouse()

    # -- win32 seams (every call guarded: a headless/failed backend must not
    #    take the whole tile down with it) ----------------------------------
    def _resolve_hwnd(self):
        try:
            return self._win32.get_root_hwnd(self.top.winfo_id())
        except Exception:
            return 0

    def _assert_ex_style(self):
        """Re-assert WS_EX_TOOLWINDOW|NOACTIVATE / clear WS_EX_APPWINDOW.
        Called at ctor, after a REAL set_alpha change (Tk's -alpha rewrites the
        whole extended-style word on the opaque<->layered transition) and on
        show(). The backend's compare-before-set gate makes a no-change
        re-assert one GetWindowLong read."""
        try:
            self._win32.exclude_from_alt_tab(self._hwnd)
        except Exception:
            pass

    def _w32_place(self, x, y, w, h):
        try:
            self._win32.set_window_pos(self._hwnd, x, y, w, h)
        except Exception:
            pass

    def _w32_retop(self):
        try:
            self._win32.retop(self._hwnd)
        except Exception:
            pass

    # -- placement / visibility ------------------------------------------------
    def place(self, x, y, w, h) -> None:
        """Position and size the tile in PHYSICAL px, flooring the size through
        preview_layout.clamp_size. Records the rect but does NOT map: show()
        owns mapping (and therefore owns the single retop)."""
        # clamp_size's params are (w, body_h) for preview; here the second is a
        # FULL height and the floors are passed explicitly.
        w, h = preview_layout.clamp_size(w, h, self.MIN_W, self.MIN_H)
        x, y = _as_int(x), _as_int(y)
        self._pos = (x, y)
        self._w, self._h = w, h
        self._w32_place(x, y, w, h)

    def rect(self) -> tuple[int, int, int, int]:
        """Current (x, y, w, h) in physical px -- h INCLUDES the strip."""
        return (self._pos[0], self._pos[1], self._w, self._h)

    def show(self) -> None:
        """Map the tile: one deiconify, one ex-style assert, one retop.

        Idempotent ON PURPOSE. A controller that calls show() on its beat must
        not turn the retop into a per-tick SetWindowPos -- re-topping every tick
        is what measurably thrashed the DWM compositor and cost in-game FPS."""
        if self._visible:
            return
        self._visible = True
        try:
            self.top.deiconify()
            self.top.update_idletasks()
        except tk.TclError:
            pass
        self._assert_ex_style()
        self._w32_place(self._pos[0], self._pos[1], self._w, self._h)
        self._w32_retop()

    def hide(self) -> None:
        """Withdraw without destroying: the layout and every child widget
        survive, so show() re-maps instantly. Idempotent."""
        if not self._visible:
            return
        self._visible = False
        try:
            self.top.withdraw()
        except tk.TclError:
            pass

    def destroy(self) -> None:
        self._visible = False
        try:
            self.top.destroy()
        except tk.TclError:
            pass

    # -- guarded setters -------------------------------------------------------
    def set_title(self, text: str) -> None:
        """Write the strip title, skipping an unchanged value.

        The controller re-pushes the same title on every beat (the intel tile's
        title carries its live filter), and an unchanged Tk ``configure`` still
        costs a full Tcl round-trip. The mirror latches only after the write
        succeeds, so a TclError is retried on the next call."""
        text = "" if text is None else str(text)
        if text == self._title_applied:
            return
        try:
            self._title_lbl.configure(text=text)
        except tk.TclError:
            return                      # never record a failed write
        self._title_applied = text

    def set_alpha(self, a: float) -> None:
        """Push the window alpha, skipping the write when it has not moved.

        The compare-before-set gate matters because the controller pushes the
        configured opacity on every beat. A REAL change is followed by an
        ex-style re-assert: Tk's ``-alpha`` rewrites GWL_EXSTYLE wholesale on
        the opaque<->layered transition and wipes WS_EX_TOOLWINDOW, which would
        drop the tile straight back into Alt-Tab. An unchanged value cannot have
        wiped anything, and show() already heals the post-map case, so the skip
        path does no win32 work at all."""
        if a == self._alpha:
            return
        try:
            self.top.attributes("-alpha", a)
        except tk.TclError:
            return                      # mirror unmoved -> the next call retries
        self._alpha = a
        self._assert_ex_style()

    def set_lock_layout(self, flag: bool) -> None:
        """When True, strip-drag, right-drag and corner-resize are all no-ops.
        The close glyph deliberately keeps working -- locking a layout must not
        trap a tile on screen. Disarms any armed corner so the resize cursor
        does not linger as a lie."""
        self._lock_layout = bool(flag)
        if self._lock_layout and self._corner is not None:
            self._corner = None
            self._set_cursor("")

    def configure_snap(self, enabled: bool, threshold: int | None = None,
                       neighbor_provider=None, screens_provider=None) -> None:
        """Enable/disable magnetic edge-snapping and (re)wire its providers.

        ``None`` means LEAVE UNCHANGED for all three optional arguments, not
        "clear": the controller pushes ``configure_snap(flag)`` live from config
        on its beat, and a None-clears reading would silently unwire the
        providers set at spawn.

        ``neighbor_provider`` returns other tiles' rects and ``screens_provider``
        the desktop rects, both as (x, y, w, h) with FULL heights -- see the
        module docstring's note about preview's body-height convention."""
        self._snap_enabled = bool(enabled)
        if threshold is not None:
            try:
                self._snap_threshold = int(threshold)
            except (TypeError, ValueError, OverflowError):
                pass
        if neighbor_provider is not None:
            self._neighbor_provider = neighbor_provider
        if screens_provider is not None:
            self._screens_provider = screens_provider

    # -- snapping --------------------------------------------------------------
    def _maybe_snap(self, x, y):
        """Snap a candidate drag position to nearby tiles' edges and to the
        desktop borders when snapping is on, else return it unchanged.

        The two providers are consulted INDEPENDENTLY, so a raising or malformed
        one costs only its own candidates -- never the other's, and never the
        drag itself."""
        if not self._snap_enabled:
            return x, y
        others = self._provider_rects(self._neighbor_provider)
        screens = self._provider_rects(self._screens_provider)
        if not others and not screens:
            return x, y
        return preview_layout.snap_rect((x, y, self._w, self._h), others,
                                        self._snap_threshold, screens=screens)

    @staticmethod
    def _provider_rects(provider):
        """Call an optional rect provider and return its well-formed rects as
        int 4-tuples. A missing/raising provider yields []; one malformed rect
        is skipped rather than poisoning the whole drag."""
        if provider is None:
            return []
        try:
            # list() inside the guard on purpose: a provider that returns a
            # non-iterable (or a generator that raises part-way) must cost only
            # its own candidates, exactly like one that raises outright.
            raw = list(provider() or [])
        except Exception:
            return []
        out = []
        for r in raw:
            try:
                a, b, c, d = r
                out.append((int(a), int(b), int(c), int(d)))
            except (TypeError, ValueError, OverflowError):
                continue
        return out

    # -- mouse model -----------------------------------------------------------
    def _bind_mouse(self):
        """Right-drag, corner-hover and corner-drag are bound on the TOPLEVEL,
        not per widget: Tk dispatches a child's event through the toplevel
        bindtag too, so every widget a renderer later packs into ``body``
        inherits the gestures without this module knowing they exist.

        The strip's LEFT-drag is bound on the strip cluster only, because a left
        press on the body must not move the tile (body content is hoverable /
        clickable). The strip handlers run on the widget bindtag -- i.e. BEFORE
        the toplevel's -- so they defer to an armed corner themselves."""
        self.top.bind("<Button-3>", self._on_b3_press)
        self.top.bind("<B3-Motion>", self._on_b3_motion)
        self.top.bind("<ButtonRelease-3>", self._on_b3_release)
        self.top.bind("<Motion>", self._on_corner_motion)
        self.top.bind("<Button-1>", self._on_body_b1_press)
        self.top.bind("<B1-Motion>", self._corner_motion)
        self.top.bind("<ButtonRelease-1>", self._corner_release)

        for w in (self._strip, self._title_lbl):
            w.bind("<ButtonPress-1>", self._on_strip_b1_press)
            w.bind("<B1-Motion>", self._on_strip_b1_motion)
            w.bind("<ButtonRelease-1>", self._on_strip_b1_release)

        # The close glyph sits inside the top-RIGHT corner zone, so its own
        # handlers return "break": that stops the remaining bindtags (including
        # the toplevel's corner arming) and keeps a click on the glyph a close
        # rather than the start of a resize.
        self._close_lbl.bind("<Motion>", self._on_close_motion)
        self._close_lbl.bind("<Enter>", self._on_close_enter)
        self._close_lbl.bind("<Leave>", self._on_close_leave)
        self._close_lbl.bind("<Button-1>", self._on_close_press)
        self._close_lbl.bind("<ButtonRelease-1>", self._on_close_release)

    # move gestures (shared implementation, one anchor per gesture) ------------
    def _move_press(self, event, slot):
        slot.root = (event.x_root, event.y_root)
        # Anchor on self._pos (the authoritative last-placed PHYSICAL top-left),
        # never winfo_root* -- Tk geometry is logical px under PMv2.
        slot.pos = self._pos
        slot.moving = False

    def _move_motion(self, event, slot):
        if slot.root is None or self._lock_layout:
            return
        dx = event.x_root - slot.root[0]
        dy = event.y_root - slot.root[1]
        if not slot.moving:
            if abs(dx) <= _MOVE_JITTER and abs(dy) <= _MOVE_JITTER:
                return                  # still a click, not a drag
            slot.moving = True
        x, y = self._maybe_snap(slot.pos[0] + dx, slot.pos[1] + dy)
        self._pos = (x, y)
        self._w32_place(x, y, self._w, self._h)

    def _move_release(self, event, slot):
        root, moving = slot.root, slot.moving
        slot.root, slot.moving = None, False
        if root is None or not moving or self._lock_layout:
            return
        dx = event.x_root - root[0]
        dy = event.y_root - root[1]
        # Re-apply the SAME snap so the committed position matches the magnetic
        # preview the user saw during the drag.
        x, y = self._maybe_snap(slot.pos[0] + dx, slot.pos[1] + dy)
        self._pos = (x, y)
        self._w32_place(x, y, self._w, self._h)
        if (x, y) != tuple(slot.pos):
            self._on_move_end(self._key, x, y, self._w, self._h)

    def _on_b3_press(self, event):
        self._move_press(event, self._b3_move)

    def _on_b3_motion(self, event):
        self._move_motion(event, self._b3_move)

    def _on_b3_release(self, event):
        self._move_release(event, self._b3_move)

    def _on_strip_b1_press(self, event):
        # A press on an armed corner starts a resize, not a strip move.
        if self._corner is not None and not self._lock_layout:
            self._corner_press(event)
            self._strip_move.root = None
            return
        self._move_press(event, self._strip_move)

    def _on_strip_b1_motion(self, event):
        if self._corner_resizing:
            self._corner_motion(event)
            return
        self._move_motion(event, self._strip_move)

    def _on_strip_b1_release(self, event):
        if self._corner_resizing:
            self._corner_release(event)
            return
        self._move_release(event, self._strip_move)

    # corner resize (the opposite corner is the fixed anchor) ------------------
    def _detect_corner(self, event):
        """Which corner (if any) the pointer is over, from the pointer position
        relative to the tile's PHYSICAL top-left. Matches the move/resize
        gestures, which also delta event.x_root against self._pos."""
        w, h = self._w, self._h
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
        """Plain hover: arm the corner under the pointer and swap the cursor.
        Moving off every corner disarms. Cheap on the unchanged path -- this
        runs for every pointer motion anywhere over the tile."""
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

    def _on_body_b1_press(self, event):
        """A left press anywhere else on the tile only ever means "start a
        corner resize" -- info tiles have no activate/click semantics."""
        if self._corner is not None and not self._lock_layout:
            self._corner_press(event)

    def _corner_press(self, event):
        """Begin a corner resize: capture the OPPOSITE corner as a fixed anchor
        (physical px) and enter resize mode. Idempotent, because both the strip
        handler and the toplevel handler can see the same press."""
        if self._lock_layout or self._corner is None or self._corner_resizing:
            return
        x, y = self._pos
        w, h = self._w, self._h
        ax = x if self._corner in ("ne", "se") else x + w
        ay = y if self._corner in ("sw", "se") else y + h
        self._corner_anchor = (ax, ay)
        self._corner_press_root = (event.x_root, event.y_root)
        self._corner_press_size = (w, h)
        self._corner_press_pos = self._pos
        self._corner_resizing = True

    def _corner_motion(self, event):
        if not self._corner_resizing or self._lock_layout:
            return
        dx = event.x_root - self._corner_press_root[0]
        dy = event.y_root - self._corner_press_root[1]
        w0, h0 = self._corner_press_size
        # The grabbed edge follows the pointer; the opposite one is anchored, so
        # west/north grabs invert the delta (drag right/down shrinks them).
        w = w0 - dx if self._corner in ("nw", "sw") else w0 + dx
        h = h0 - dy if self._corner in ("nw", "ne") else h0 + dy
        w, h = preview_layout.clamp_size(w, h, self.MIN_W, self.MIN_H)
        ax, ay = self._corner_anchor
        x = ax if self._corner in ("ne", "se") else ax - w
        y = ay if self._corner in ("sw", "se") else ay - h
        self._w, self._h = w, h
        self._pos = (x, y)
        self._w32_place(x, y, w, h)

    def _corner_release(self, event):
        if not self._corner_resizing:
            return
        self._corner_resizing = False
        self._corner_anchor = None
        self._corner_press_root = None
        self._corner_press_size = None
        press_pos = self._corner_press_pos
        self._corner_press_pos = None
        if self._lock_layout:
            return
        x, y, w, h = self.rect()
        # An nw/ne/sw grab anchors the OPPOSITE corner, so the top-left MOVED
        # and has to be reported too or the controller re-persists the
        # pre-resize origin. An 'se' grab moves nothing and must not report a
        # move it did not make.
        if press_pos is not None and self._pos != press_pos:
            self._on_move_end(self._key, x, y, w, h)
        self._on_resize_end(self._key, x, y, w, h)

    # close glyph --------------------------------------------------------------
    def _on_close_enter(self, _event):
        self._corner = None             # the glyph sits inside the 'ne' zone
        self._set_cursor("")
        try:
            self._close_lbl.configure(fg=self._fg_close_hot)
        except tk.TclError:
            pass
        return "break"

    def _on_close_leave(self, _event):
        self._close_pressed = False
        try:
            self._close_lbl.configure(fg=self._fg_dim)
        except tk.TclError:
            pass
        return "break"

    def _on_close_motion(self, _event):
        self._corner = None
        return "break"

    def _on_close_press(self, _event):
        self._close_pressed = True
        self._strip_move.root = None    # a glyph press is never a strip drag
        return "break"

    def _on_close_release(self, _event):
        """Report the close intent and nothing else. The tile NEVER destroys
        itself: the controller owns lifecycle (it also has to persist the
        tile's disabled state), and a self-destroying window would leave the
        controller holding a dead handle."""
        if not self._close_pressed:
            return "break"
        self._close_pressed = False
        self._on_close(self._key)
        return "break"
