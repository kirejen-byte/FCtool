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

# ── on-video activity label (caption-onvideo) ────────────────────────────────
# The activity label ("<label> - <ShipType>") is drawn directly on the tile BODY
# (over the DWM thumbnail) at a configurable corner, as a black 8-direction
# outline with a coloured fill on top — the same legibility trick eveo_overlay
# uses, so the text reads on both bright and dark video.
_LABEL_OUTLINE = "#000000"
_LABEL_OUTLINE_OFFSET = 1
# The 8 surrounding 1px positions (N/S/E/W + 4 diagonals) forming the halo.
_LABEL_OUTLINE_OFFSETS = [
    (-1, -1), (0, -1), (1, -1),
    (-1, 0),          (1, 0),
    (-1, 1), (0, 1), (1, 1),
]
_LABEL_PAD = 4            # px inset from the chosen body corner
_LABEL_MIN_SIZE = 8      # sane font floor
_LABEL_MAX_SIZE = 28     # sane font ceiling (before the body-fraction cap)
_LABEL_MAX_CHARS = 40    # hard character cap before the width-based ellipsis
# Rough monospace char-advance as a fraction of font point size. Consolas at N pt
# advances ~0.6*N px per glyph; used to estimate rendered width Tk-free so the
# clamp/ellipsis helpers stay pure (no font metrics / no Tk).
_CHAR_W_RATIO = 0.62


# ── damage-flash soft pulse ──────────────────────────────────────────────────
# The damage flash PULSES: the border eases between a soft red and the peak
# colour at ~2 Hz (period ~0.5 s) while active. pulse_color is pure/Tk-free so it
# is unit-testable; the tick steps `elapsed_s` and pushes the result via
# tile.set_border. On-video render + the pulse *feel* need a live re-test (FLAG).
_PULSE_SOFT_FLOOR = 0.45     # softest channel scale at the trough (never black)

# ── decloak hazard banner ────────────────────────────────────────────────────
# When a tracked character is decloaked, the bottom label strip is REPLACED for
# the alert window by a classic hazard banner: black bold text on a yellow strip,
# the DECLOAKED word flanked by hazard-bar glyphs. The measure-based auto-fit
# (_fit_label_text) still applies so it never overflows a narrow tile.
_ALERT_BG = "#ffcc00"        # hazard yellow strip background
_ALERT_FG = "#000000"        # black text (classic hazard look)
DECLOAK_BANNER_TEXT = "▞▞▞ DECLOAKED ▞▞▞"


def _clamp01(x: float) -> float:
    return 0.0 if x < 0.0 else (1.0 if x > 1.0 else x)


def pulse_color(peak_hex, elapsed_s, period_s) -> str:
    """Ease a border colour between the peak (`peak_hex`) and a softer version of
    it, as a cosine pulse of period `period_s`. At elapsed 0 (and every whole
    period) the colour sits at the PEAK; at the half-period it sits at the
    softest point (each channel scaled by _PULSE_SOFT_FLOOR). Pure/Tk-free.

    Robust to bad input: a non-#rrggbb `peak_hex` falls back to a default red; a
    zero/negative period holds at peak; elapsed is treated by its phase only.
    Always returns a valid '#rrggbb' string."""
    h = (peak_hex or "").strip().lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    try:
        r = int(h[0:2], 16)
        g = int(h[2:4], 16)
        b = int(h[4:6], 16)
    except (ValueError, IndexError):
        r, g, b = 0xff, 0x3b, 0x30       # default soft red peak
    try:
        period = float(period_s)
    except (TypeError, ValueError):
        period = 0.0
    if period <= 0.0:
        scale = 1.0                      # no pulse → hold at peak
    else:
        import math
        try:
            phase = float(elapsed_s) / period
        except (TypeError, ValueError):
            phase = 0.0
        # cosine pulse: 1.0 at phase 0/1/2…, dips to the soft floor at 0.5.
        wave = (1.0 + math.cos(2.0 * math.pi * phase)) / 2.0   # 1→0→1
        scale = _PULSE_SOFT_FLOOR + (1.0 - _PULSE_SOFT_FLOOR) * wave
    scale = _clamp01(scale)
    r = int(round(_clamp01(r / 255.0 * scale) * 255))
    g = int(round(_clamp01(g / 255.0 * scale) * 255))
    b = int(round(_clamp01(b / 255.0 * scale) * 255))
    return f"#{r:02x}{g:02x}{b:02x}"


def format_tile_label(activity_label, ship_type_name) -> str:
    """Join the activity label and ship TYPE name as '<label> - <ShipType>',
    omitting empty/whitespace-only parts.

    label only -> 'Cyno'; ship only -> 'Onyx'; both -> 'Cyno - Onyx';
    neither -> '' (caller hides the label entirely). When both parts are present
    and equal case-insensitively (e.g. a rule labels an Onyx hull "Onyx"), the
    value is shown ONCE -> 'Onyx' rather than 'Onyx - Onyx'. Pure/Tk-free."""
    parts = [p.strip() for p in (activity_label, ship_type_name)
             if p and str(p).strip()]
    if len(parts) == 2 and parts[0].casefold() == parts[1].casefold():
        return parts[0]
    return " - ".join(parts)


def _ellipsize(text: str, max_chars: int) -> str:
    """Truncate `text` to at most `max_chars` glyphs, appending '…' when cut.
    max_chars <= 0 yields '' (nothing fits)."""
    if max_chars <= 0:
        return ""
    if len(text) <= max_chars:
        return text
    if max_chars == 1:
        return "…"
    return text[:max_chars - 1] + "…"


def clamp_label(text, font_size, tile_w, tile_body_h):
    """Bound an on-video label so it can never overflow or fill the tile.

    Returns (clamped_text, clamped_size):
      - size is clamped to [_LABEL_MIN_SIZE, _LABEL_MAX_SIZE] AND to at most
        ~tile_body_h/5 so a huge font can't dominate the body;
      - text is ellipsized to _LABEL_MAX_CHARS AND to the body width (estimated
        from the clamped size), truncating with '…'.

    Pure/Tk-free: width is estimated via _CHAR_W_RATIO, not real font metrics
    (live rendering is re-tested on-video). Deterministic for unit tests."""
    text = text or ""
    try:
        size = int(font_size)
    except (TypeError, ValueError):
        size = _LABEL_MIN_SIZE
    # cap by the sane range first, then by the body fraction (never below floor).
    size = max(_LABEL_MIN_SIZE, min(size, _LABEL_MAX_SIZE))
    if tile_body_h and tile_body_h > 0:
        body_cap = max(_LABEL_MIN_SIZE, int(tile_body_h // 5))
        size = min(size, body_cap)
    # hard character cap first (cheap, independent of width).
    text = _ellipsize(text, _LABEL_MAX_CHARS)
    # then the width cap: how many glyphs fit inside (tile_w - 2*pad)?
    if tile_w and tile_w > 0:
        usable = max(0, tile_w - 2 * _LABEL_PAD)
        per_char = max(1.0, size * _CHAR_W_RATIO)
        max_chars_w = int(usable // per_char)
        if max_chars_w < len(text):
            text = _ellipsize(text, max_chars_w)
    return text, size


def label_anchor_placement(anchor, pad_frac=0.0):
    """Map an overlay anchor name to (relx, rely, tk_anchor) for place()-ing the
    on-video label canvas at a BODY corner. `relx`/`rely` are relative positions
    in [0,1]; `tk_anchor` is the Tk anchor that pins the widget's own corner to
    that point. Unknown anchors fall back to top-left. Pure/Tk-free."""
    a = (anchor or "").strip().lower()
    lo, hi = pad_frac, 1.0 - pad_frac
    table = {
        "top-left": (lo, lo, "nw"),
        "top-right": (hi, lo, "ne"),
        "bottom-left": (lo, hi, "sw"),
        "bottom-right": (hi, hi, "se"),
    }
    return table.get(a, (lo, lo, "nw"))


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
                 on_snap_others=None, lock_layout=False):
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
        # Snap-to-neighbours: provider returns OTHER tiles' rects (x, y, w, body_h)
        # from the host's hwnd-keyed _preview_tile_rects, self already excluded.
        self._on_snap_others = on_snap_others
        self._snap_enabled = False        # pushed live via configure_snap
        self._snap_threshold = preview_layout.SNAP_THRESHOLD_PX
        self._excluded = False        # session-only cycle-exclusion flag (C4)

        self._thumb = None
        self._src_size = (0, 0)
        self._w = 0
        self._body_h = 0

        # on-video activity label style (caption-onvideo). Fed live from
        # config['overlay'] color/font_size/anchor via set_label_style; the strip
        # no longer carries the activity label (bug (i)/(ii) fix).
        self._label_color = "#ffffff"
        self._label_size = 11
        self._label_anchor = "top-left"
        self._label_text = ""
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
        # Normal bottom-strip background, restored when a decloak alert clears
        # (set_bottom_alert paints it hazard-yellow; set_bottom_label restores it).
        self._bg_panel = bg_panel

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

        # ── bottom caption strip (mirror of the top strip) ──────────────────
        # The activity label ('<label> - <ShipType>') lives in a small strip
        # BELOW the video, never over it. The old on-video child-canvas is
        # DWM-occluded and the topmost-overlay workaround caused in-game lag, so
        # the label now draws here. Packed side="bottom" BEFORE the body so the
        # body expands into the middle, between the top strip and this one; the
        # window total height (body_h + STRIP_H) is unchanged — the strip is
        # carved out of the body area via _push_thumb_rect's avail_h.
        self._strip_bottom = tk.Frame(self.top, bg=bg_panel, height=STRIP_H)
        self._strip_bottom.pack_propagate(False)
        self._bottom_lbl = tk.Label(self._strip_bottom, text="", bg=bg_panel,
                                    fg=fg_text, anchor="w",
                                    font=("Consolas", 9, "bold"))
        self._bottom_lbl.pack(side="left", fill="x", padx=(4, 4))
        self._bottom_visible = False   # True while the strip is packed/shown
        self._bottom_strip_h = STRIP_H  # last chosen (clamped) bottom-strip height
        self._bottom_alert = False     # True while the strip shows the decloak banner

        # ── location caption strip (its OWN thin line, LOWEST of all) ────────
        # The pilot's current system/location gets its own strip packed BELOW the
        # activity-label strip so neither existing line is crowded. Same look as
        # the label strip; carved from the body via _push_thumb_rect just like it.
        self._strip_location = tk.Frame(self.top, bg=bg_panel, height=STRIP_H)
        self._strip_location.pack_propagate(False)
        self._location_lbl = tk.Label(self._strip_location, text="", bg=bg_panel,
                                      fg=fg_text, anchor="w",
                                      font=("Consolas", 9, "bold"))
        self._location_lbl.pack(side="left", fill="x", padx=(4, 4))
        self._location_visible = False   # True while the strip is packed/shown
        self._location_strip_h = STRIP_H  # last chosen (clamped) location-strip height

        # ── body (DWM composites the live thumbnail over this) ──────────────
        self._body = tk.Frame(self.top, bg="#000000")
        self._body.pack(fill="both", expand=True, side="top")

        # On-video activity label (RETIRED): the DWM compositor draws each tile's
        # live thumbnail OVER any child widget inside the thumbnail rect, so this
        # child Canvas was occluded, and routing the label through a separate
        # topmost overlay caused massive in-game FPS drops. The activity label now
        # renders in the bottom strip (see set_bottom_label). This canvas + the
        # _draw_video_label path are kept as dead code so references never break;
        # set_video_label is now a no-op.
        self._label_canvas = tk.Canvas(
            self._body, bg="#000000", highlightthickness=0, bd=0,
            width=1, height=1)
        self._label_placed = False

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
                  self._chip_lbl, self._tag_lbl, self._dot,
                  self._strip_bottom, self._bottom_lbl,
                  self._strip_location, self._location_lbl):
            w.bind("<Button-3>", self._on_b3_press)
            w.bind("<B3-Motion>", self._on_b3_motion)
            w.bind("<ButtonRelease-3>", self._on_b3_release)
        # LEFT on the BODY (and toplevel) = activate/minimize/etc (click semantics).
        for w in (self.top, self._body):
            w.bind("<Button-1>", self._on_b1_press)
            w.bind("<ButtonRelease-1>", self._on_b1_release)
        # LEFT on the CAPTION STRIP = title-bar drag-to-move; a plain click there
        # still activates (BUG B). The strip cluster (top strip + bottom strip +
        # their child labels) gets the strip handlers so a left-drag anywhere on
        # either caption bar moves the tile (bottom strip = same, no dead zone).
        for w in (self._strip, self._name_lbl, self._excl_lbl, self._chip_lbl,
                  self._tag_lbl, self._dot,
                  self._strip_bottom, self._bottom_lbl,
                  self._strip_location, self._location_lbl):
            w.bind("<ButtonPress-1>", self._on_strip_b1_press)
            w.bind("<B1-Motion>", self._on_strip_b1_motion)
            w.bind("<ButtonRelease-1>", self._on_strip_b1_release)
        # Corner-hover resize: a plain <Motion> over any strip/body widget arms
        # the nearest corner (12 px zone) and swaps the cursor to the matching
        # diagonal-resize glyph. The armed corner + a left press start a resize.
        for w in (self.top, self._strip, self._body, self._name_lbl,
                  self._excl_lbl, self._chip_lbl, self._tag_lbl, self._dot,
                  self._strip_bottom, self._bottom_lbl,
                  self._strip_location, self._location_lbl):
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

    def configure_snap(self, enabled, threshold=None):
        """Enable/disable magnetic edge-snapping against other tiles while
        dragging, and optionally set the snap threshold (px). The controller
        pushes this live from config['preview']['snap_enabled'] on every tick via
        _preview_style_tile — exactly like set_lock_layout / configure_hover — so
        a live toggle takes effect without respawning the tile. `threshold` has
        no UI (it defaults to the preview_layout module constant)."""
        self._snap_enabled = bool(enabled)
        if threshold is not None:
            try:
                self._snap_threshold = int(threshold)
            except (TypeError, ValueError):
                pass

    def _maybe_snap(self, x, y):
        """Snap a candidate drag position (x, y) to nearby OTHER tiles' edges when
        snapping is enabled, else return it unchanged.

        Neighbour rects come from the injected on_snap_others provider — the
        host's _preview_tile_rects snapshot, which stores (x, y, w, BODY_H) with
        self already excluded. Both the moving rect and every neighbour are
        converted to FULL window height (body_h + STRIP_H) before snapping, so a
        top/bottom butt sticks to the tile's VISIBLE edge (the strip edge) with
        zero gap — using body_h alone would leave a STRIP_H gap/overlap. Pure
        position math; the caller applies the result via set_window_pos /
        on_move_end."""
        if not self._snap_enabled or self._on_snap_others is None:
            return x, y
        try:
            others = self._on_snap_others() or []
        except Exception:
            return x, y
        full = []
        for o in others:
            try:
                ox, oy, ow, oh = o
            except (TypeError, ValueError):
                continue
            full.append((int(ox), int(oy), int(ow), int(oh) + STRIP_H))
        if not full:
            return x, y
        moving = (x, y, self._w, self._body_h + STRIP_H)
        return preview_layout.snap_rect(moving, full, self._snap_threshold)

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
        x, y = self._maybe_snap(x, y)     # magnetic edge-snap (no-op when off)
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
            # Re-apply the SAME snap so the committed/persisted position matches
            # the magnetic preview the user saw during the drag (on_move_end
            # remains the sole write-back — its _preview_tile_rects/save contract
            # is unchanged; only the coords it receives are snapped).
            x, y = self._maybe_snap(x, y)
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
            x, y = self._maybe_snap(x, y)     # magnetic edge-snap (no-op when off)
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
            x, y = self._maybe_snap(x, y)     # persist the snapped position (see strip release)
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
        self._hidden = False   # deiconify() below un-hides the tile; keep the flag honest
        self.top.deiconify()
        self._win32.set_window_pos(self._hwnd, x, y, w, body_h + STRIP_H)
        self._push_thumb_rect()

    def body_screen_rect(self):
        """(left, top, right, bottom) of the BODY region (below the caption strip)
        in PHYSICAL screen px, or None if unplaced/hidden. Uses self._pos + STRIP_H
        (the authoritative physical geometry), NOT winfo_root* (logical under PMv2)."""
        if getattr(self, "_hidden", False):
            return None
        if self._w <= 0 or self._body_h <= 0:
            return None
        x, y = self._pos
        return (x, y + STRIP_H, x + self._w, y + STRIP_H + self._body_h)

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

    def _bottom_h(self) -> int:
        """Total height in px of ALL bottom strips currently visible (activity
        label + location line), else 0. Each strip is carved out of the body area
        (the window total height stays body_h + STRIP_H), so the letterboxed video
        shrinks by exactly this sum.

        The combined height is capped at ~60% of the body as a safety net so the
        two bottom lines can never swallow the whole video, even if both request
        their per-strip 40% cap on a squat tile."""
        total = (self._bottom_strip_h if self._bottom_visible else 0)
        total += (self._location_strip_h if self._location_visible else 0)
        if self._body_h and self._body_h > 0:
            total = min(total, int(self._body_h * 0.60))
        return total

    def _push_thumb_rect(self):
        if not self._thumb:
            return
        # Letterbox the video into the body MINUS both bottom strips, so neither
        # strip ever overlaps the live thumbnail (both sit below it).
        avail_h = max(1, self._body_h - self._bottom_h())
        fx, fy, fw, fh = aspect_fit(self._w, avail_h, *self._src_size)
        self._thumb.show((fx, STRIP_H + fy, fx + fw, STRIP_H + fy + fh))

    def refresh_source_size(self):
        """Called by the tick every ~8th cycle: re-letterbox if the client resized."""
        if self._thumb:
            size = self._thumb.source_size()
            if size != self._src_size:
                self._src_size = size
                self._push_thumb_rect()

    @property
    def key(self):
        """Read-only layout key for this tile. fc_gui's arrange/visibility code
        matches tiles via getattr(tile, "key", None) == client.key, so this must
        stay public and mirror self._key (updated live by set_key)."""
        return self._key

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
        # The strip carries only name + status dot + role chip now; the activity
        # label ('tag') has moved onto the video body (caption-onvideo). Ellipsize
        # the NAME so a long name can't push the chip off the fixed-width row.
        shown = self._badge or self._name
        shown = self._ellipsize_name(shown)
        try:
            self._name_lbl.configure(text=shown)
            self._chip_lbl.configure(text=self._chip)
            self._tag_lbl.configure(text="")     # activity moved on-video
        except tk.TclError:
            pass

    def _ellipsize_name(self, name):
        """Truncate the strip name to fit the fixed-width row. Budget = the tile
        width minus the dot/chip/exclusion glyphs and padding, estimated Tk-free
        via the strip's 9pt name font. Never returns more chars than fit."""
        name = name or ""
        w = self._w if self._w > 0 else 0
        if w <= 0:
            return name
        # reserve space for the dot (~17px), chip (~len*7px + pad), excl (~14px)
        chip_px = (len(self._chip) + 1) * 8 if self._chip else 0
        reserved = 17 + 14 + chip_px + 8
        usable = max(0, w - reserved)
        per_char = 9 * _CHAR_W_RATIO       # 9pt bold Consolas ≈ this px/glyph
        budget = int(usable // max(1.0, per_char))
        return _ellipsize(name, budget)

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

    # ── on-video activity label (caption-onvideo) ───────────────────────────
    def set_label_style(self, color=None, size=None, anchor=None):
        """Push the on-video label style from config['overlay'] (color=fill,
        size=font_size, anchor=corner). Editing these in settings and calling
        this on every existing tile makes style edits update live (fixes bug
        (ii)). Re-draws the current label with the new style immediately."""
        if color is not None:
            self._label_color = color
        if size is not None:
            try:
                self._label_size = int(size)
            except (TypeError, ValueError):
                pass
        if anchor is not None:
            self._label_anchor = anchor
        self._draw_video_label()

    def set_video_label(self, text):
        """RETIRED no-op. The on-video label drew over the DWM thumbnail (which
        occludes it) and the topmost-overlay workaround caused in-game lag, so the
        activity label now renders in the bottom strip (set_bottom_label). The
        method + signature are kept so callers never break; it does NOT draw."""
        self._label_text = text or ""

    def video_label_text(self) -> str:
        """The last text handed to the retired set_video_label — for tests/logging.
        (The visible activity label now lives in the bottom strip.)"""
        return self._label_text

    # ── bottom caption strip label ('<label> - <ShipType>', below the video) ──
    def set_bottom_label(self, text, color=None, size=None):
        """Show the activity label ('<label> - <ShipType>') in the bottom strip,
        BELOW the video (never over it). This replaces the retired on-video label.

        Empty/falsy text hides the strip and lets the video reclaim the space.
        A non-empty text ellipsizes to the tile width, applies the given fill
        colour + font size (from config['overlay'] color/font_size), sizes the
        strip to the font (clamped so it can never exceed ~40% of the body), packs
        it if hidden, and re-letterboxes the thumbnail into the smaller area.

        RESTORE PATH: this always repaints the strip/label to the normal panel
        background + given fg and clears the decloak-alert flag, so after a
        decloak banner's window expires the next compose-tick call here brings the
        normal 'Label - ShipType' text and panel bg back automatically — a yellow
        strip is never left behind."""
        text = "" if not text else str(text)
        if not text:
            if self._bottom_visible:
                try:
                    self._strip_bottom.pack_forget()
                except tk.TclError:
                    pass
                self._bottom_visible = False
                self._push_thumb_rect()   # video reclaims the freed space
            self._bottom_alert = False    # strip is gone → no alert lingering
            return
        # Resolve style. Fall back to the strip's default fg / the top-strip name
        # font size (9) when the caller passes nothing.
        fill = color if color else self._fg_text
        try:
            fsize = int(size) if size is not None else 9
        except (TypeError, ValueError):
            fsize = 9
        fsize = max(_LABEL_MIN_SIZE, min(fsize, _LABEL_MAX_SIZE))
        # Auto-fit: keep the chosen font size when the text fits the tile width,
        # shrink it only as needed (measured with real font metrics), and ellipsize
        # only as a last resort at the floor. Replaces the old char-count estimate.
        shown, drawn_size = self._fit_label_text(text, fsize)
        # Strip height tracks the DRAWN (possibly downscaled) font so a bigger font
        # stays legible, but is CLAMPED to ~40% of the body so it can't swallow the
        # video.
        strip_h = max(STRIP_H, drawn_size + 8)
        if self._body_h and self._body_h > 0:
            cap = max(STRIP_H, int(self._body_h * 0.40))
            strip_h = min(strip_h, cap)
        self._bottom_strip_h = strip_h
        # Repaint back to the NORMAL panel bg (undo any prior hazard-yellow) and
        # clear the alert flag — this is the explicit banner->normal restore.
        self._bottom_alert = False
        try:
            self._strip_bottom.configure(height=strip_h, bg=self._bg_panel)
            self._bottom_lbl.configure(text=shown, fg=fill, bg=self._bg_panel,
                                       font=("Consolas", drawn_size, "bold"))
        except tk.TclError:
            pass
        if not self._bottom_visible:
            try:
                self._strip_bottom.pack(side="bottom", fill="x")
            except tk.TclError:
                pass
            self._bottom_visible = True
            # If the location line is already shown, re-pin it BELOW this label
            # strip so the location always stays the lowest line (Tk order depends
            # on pack sequence; re-packing location `before` the label fixes it).
            if self._location_visible:
                self._pack_location_lowest()
        self._push_thumb_rect()           # shrink the video above the strip

    def set_bottom_alert(self, text=DECLOAK_BANNER_TEXT, size=None):
        """Show the decloak HAZARD BANNER in the bottom strip IN PLACE OF the
        normal activity label: black bold text on a yellow strip (the classic
        hazard look). Mirrors set_bottom_label's packing / height-clamp /
        measure-based auto-fit so the banner never overflows a narrow tile, but
        forces the hazard fg/bg and marks the strip as an alert.

        The compose pass calls this every tick while the decloak window is live;
        once the window expires it calls set_bottom_label (or set_bottom_label(''))
        instead, which repaints the normal panel bg and clears the alert flag."""
        text = DECLOAK_BANNER_TEXT if not text else str(text)
        try:
            fsize = int(size) if size is not None else 9
        except (TypeError, ValueError):
            fsize = 9
        fsize = max(_LABEL_MIN_SIZE, min(fsize, _LABEL_MAX_SIZE))
        # Same auto-fit as the normal label so the banner shrinks/ellipsizes to
        # fit rather than overflowing.
        shown, drawn_size = self._fit_label_text(text, fsize)
        strip_h = max(STRIP_H, drawn_size + 8)
        if self._body_h and self._body_h > 0:
            cap = max(STRIP_H, int(self._body_h * 0.40))
            strip_h = min(strip_h, cap)
        self._bottom_strip_h = strip_h
        self._bottom_alert = True
        try:
            self._strip_bottom.configure(height=strip_h, bg=_ALERT_BG)
            self._bottom_lbl.configure(text=shown, fg=_ALERT_FG, bg=_ALERT_BG,
                                       font=("Consolas", drawn_size, "bold"))
        except tk.TclError:
            pass
        if not self._bottom_visible:
            try:
                self._strip_bottom.pack(side="bottom", fill="x")
            except tk.TclError:
                pass
            self._bottom_visible = True
            if self._location_visible:
                self._pack_location_lowest()
        self._push_thumb_rect()           # shrink the video above the strip

    def _ellipsize_bottom(self, text, font_size):
        """Truncate the bottom-strip label to fit the tile width, estimated
        Tk-free from the font size (mirrors _ellipsize_name's width budget). The
        bottom label spans nearly the full width, so only the L/R strip pad (4+4)
        is reserved."""
        text = text or ""
        w = self._w if self._w > 0 else 0
        if w <= 0:
            return text
        usable = max(0, w - 2 * _LABEL_PAD)
        per_char = max(1.0, font_size * _CHAR_W_RATIO)
        budget = int(usable // per_char)
        return _ellipsize(text, budget)

    def _fit_label_text(self, text, base_size):
        """Auto-fit a bottom-line label to the tile width. Returns
        (shown_text, drawn_size):

          - Keeps `base_size` when `text` fits inside the available width, measured
            with REAL font metrics (Consolas, bold, at each candidate size).
          - Otherwise decrements the size from `base_size` down to _LABEL_MIN_SIZE,
            picking the largest size whose measured width fits.
          - If it still doesn't fit at the floor, ellipsizes (via _ellipsize_bottom)
            at that floor size as a last resort so it can never overflow.

        `avail_w` = the tile width minus the label's L/R pad (4+4). When the width
        is unknown (unplaced) the base size + full text are returned unchanged.
        Font-metric creation is guarded against tk.TclError (headless) — on failure
        it falls back to the Tk-free char-count estimate (_ellipsize_bottom)."""
        text = text or ""
        try:
            base = int(base_size)
        except (TypeError, ValueError):
            base = _LABEL_MIN_SIZE
        base = max(_LABEL_MIN_SIZE, min(base, _LABEL_MAX_SIZE))
        w = self._w if self._w > 0 else 0
        if w <= 0 or not text:
            return text, base
        avail_w = max(0, w - 2 * _LABEL_PAD)
        try:
            import tkinter.font as tkfont
            root = getattr(self, "top", None)
            best = None
            for size in range(base, _LABEL_MIN_SIZE - 1, -1):
                f = tkfont.Font(root=root, family="Consolas", size=size,
                                weight="bold")
                if f.measure(text) <= avail_w:
                    best = size
                    break
            if best is not None:
                return text, best
            # Still too wide at the floor → ellipsize to fit at the floor size.
            floor = _LABEL_MIN_SIZE
            f = tkfont.Font(root=root, family="Consolas", size=floor,
                            weight="bold")
            lo, hi = 0, len(text)
            while lo < hi:
                mid = (lo + hi + 1) // 2
                cand = _ellipsize(text, mid)
                if f.measure(cand) <= avail_w:
                    lo = mid
                else:
                    hi = mid - 1
            return _ellipsize(text, lo), floor
        except tk.TclError:
            # Headless / no font support: fall back to the char-count estimate at
            # the base size (never raises, keeps the label bounded).
            return self._ellipsize_bottom(text, base), base

    # ── location caption strip (its own thin line, below the label strip) ─────
    def set_location_label(self, text, color=None, size=None):
        """Show the pilot's current system/location on its OWN thin strip BELOW
        the activity-label strip (the lowest line of the tile). Mirrors
        set_bottom_label.

        Empty/falsy text hides the strip and lets the video reclaim the space.
        A non-empty text applies the given fill colour + font size (from
        config['overlay'] color/font_size), AUTO-FITS the font down so it never
        overflows (keeping the chosen size when it fits), sizes the strip to the
        drawn font (clamped so it can never exceed ~40% of the body, with the
        combined bottom-strips height capped at ~60% of the body via _bottom_h),
        packs it below the label strip, and re-letterboxes the thumbnail."""
        text = "" if not text else str(text)
        if not text:
            if self._location_visible:
                try:
                    self._strip_location.pack_forget()
                except tk.TclError:
                    pass
                self._location_visible = False
                self._push_thumb_rect()   # video reclaims the freed space
            return
        fill = color if color else self._fg_text
        try:
            fsize = int(size) if size is not None else 9
        except (TypeError, ValueError):
            fsize = 9
        fsize = max(_LABEL_MIN_SIZE, min(fsize, _LABEL_MAX_SIZE))
        # Auto-fit: keep the chosen size when the location fits, shrink to fit
        # otherwise (measured), ellipsize only at the floor.
        shown, drawn_size = self._fit_label_text(text, fsize)
        strip_h = max(STRIP_H, drawn_size + 8)
        if self._body_h and self._body_h > 0:
            cap = max(STRIP_H, int(self._body_h * 0.40))
            strip_h = min(strip_h, cap)
        self._location_strip_h = strip_h
        try:
            self._strip_location.configure(height=strip_h)
            self._location_lbl.configure(text=shown, fg=fill,
                                         font=("Consolas", drawn_size, "bold"))
        except tk.TclError:
            pass
        if not self._location_visible:
            self._pack_location_lowest()
            self._location_visible = True
        self._push_thumb_rect()           # shrink the video above both strips

    def _pack_location_lowest(self):
        """Pack the location strip so it is the LOWEST bottom-side widget. When
        the activity-label strip is already packed, pack `before` it (Tk places an
        earlier-packed `side=bottom` widget nearer the edge), so the location line
        always sits under the label line regardless of call order."""
        try:
            if self._bottom_visible:
                self._strip_location.pack(side="bottom", fill="x",
                                          before=self._strip_bottom)
            else:
                self._strip_location.pack(side="bottom", fill="x")
        except tk.TclError:
            pass

    def location_label_text(self) -> str:
        """The text currently shown in the location strip ('' when hidden) — for
        tests/logging."""
        return self._location_lbl.cget("text") if self._location_visible else ""

    def bottom_label_text(self) -> str:
        """The text currently shown in the bottom strip ('' when hidden) — for
        tests/logging. Pre-ellipsis (the raw text handed to set_bottom_label)."""
        return self._bottom_lbl.cget("text") if self._bottom_visible else ""

    def is_bottom_alert(self) -> bool:
        """True while the bottom strip shows the decloak hazard banner (yellow
        strip / black text), False once a normal label or hide restores it — for
        tests/logging."""
        return bool(self._bottom_visible and self._bottom_alert)

    def _draw_video_label(self):
        """RETIRED. The on-video label is gone (DWM-occluded + the topmost-overlay
        workaround lagged the game); the activity label now renders in the bottom
        strip. This is kept as a defensive no-op that only ever ensures the dead
        _label_canvas stays unplaced — it must NEVER draw over the video again.
        (set_label_style still calls it; that call is harmless.)"""
        cv = getattr(self, "_label_canvas", None)
        if cv is None:
            return
        try:
            cv.delete("all")
            if getattr(self, "_label_placed", False):
                cv.place_forget()
        except tk.TclError:
            pass
        self._label_placed = False

    def destroy(self):
        self.detach()
        try:
            self.top.destroy()
        except tk.TclError:
            pass
