"""The text BAND a preview tile can draw over the bottom of its video.

Why a second window at all: the compositor draws a DWM thumbnail OVER its
destination window's own content, so a child widget inside the thumbnail
rectangle is invisible. Text over the picture has to come from a separate
top-level window. This module owns that window: one small popup per tile,
OWNED by the tile's hwnd.

What the owner relationship buys (measured on a hidden desktop, 2026-10-13):
the band inherits topmost from its tile and - from the tile's first re-top
after the band was created (BandWindow._create says where a NEW band lands) -
stays DIRECTLY above it through every re-top of the tile or of an
overlapping neighbour, the tile's own placement, its opacity flip and its
withdraw / deiconify - with no call made on the band. So nothing here ever
re-stacks the band. What the relationship does NOT do: the band does not move
with its tile and is not hidden when the tile is withdrawn. The tile says both through BandWindow.sync().

THE TWO FPS INVARIANTS (a change that breaks either is a regression):
  1. the band is never re-stacked by our code - the one window-position call
     in this file carries SWP_NOZORDER | SWP_NOACTIVATE;
  2. an unchanged sync() makes ZERO backend calls - the 250 ms preview tick
     costs a band nothing.
The on-video label this replaces (eveo_overlay.OverlayWindow in native mode,
retired 2026-07-03) broke both: one desktop-wide window, redrawn and re-topped
on every tick.

Layers, top to bottom of the file:
  * pure functions - text_band_wanted / text_band_effective (the 'Label
    text' switch's config rule, read by fc_gui), layout_band (row
    rectangles) and compose_pixels (glyph coverage -> premultiplied BGRA).
    No window, no ctypes; importable anywhere.
  * BandWindow - one band for one tile, every Win32 call behind an injectable
    backend (win32=), the preview_tile / eveo_overlay house pattern.
  * _RealBandWin32 - the ctypes backend, built LAZILY by _real_band_win32().
    Nothing in this module touches ctypes at import time.

No Tk import and no import of preview_tile: the tile imports the band, never
the reverse. The tile fits the text (shrink / ellipsis) and picks the row
heights with the functions its strips use, so a line reads the same in both
layouts; this module only places and paints what it is handed.

Compliance: every call here targets a window THIS process created on THIS
thread. The owner hwnd is checked before a band is created. No message,
input, focus or process-memory function is bound (tests pin the census).
"""
from __future__ import annotations

from dataclasses import dataclass

# Background alpha of a normal row: 60 % black behind the text (owner decision
# D5 - fixed, no setting). An alert row is opaque.
BAND_BG_ALPHA = 153
_ALERT_BG_ALPHA = 255

# Default colour of the text over the video: dark orange (owner decision D7).
# THE one place this hex is written - fc_gui reads it from here. Not to be
# confused with the NPC damage flash's orange, which is a different colour.
TEXT_COLOR_DEFAULT = "#ff8c00"

# The band may cover at most this fraction of the tile body - the same cap
# preview_tile._bottom_h applies to the strips the band replaces.
_BAND_MAX_FRAC = 0.60

# Horizontal text inset in px - the padx of the strips' labels.
_TEXT_PAD = 4

# Bold Consolas' GDI cell height : em height, as 2398 : 2048 (see
# _glyph_px_max - the ratio that keeps a drawn line inside its row).
_CELL_EM_NUM, _CELL_EM_DEN = 2398, 2048

_FG_FALLBACK = (255, 255, 255)      # a garbage text colour draws white
_BG_FALLBACK = (0, 0, 0)            # a garbage background draws black

# The ONLY characters a '#rrggbb' colour may hold after the '#'. Explicit and
# ASCII: int(s, 16) also takes a sign, spaces, underscores and non-ASCII digits.
_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")

# A band size (tile width, body height) larger than this is garbage: no
# monitor is that big, and int(body_h * 0.60) would overflow on 10 ** 400.
_MAX_SIZE_PX = 1 << 20

# The 'Label text' switch (preview.text_placement): the one value that asks
# for the band. See text_band_wanted.
TEXT_PLACEMENT_BAND = "over_video"


def text_band_wanted(cfg) -> bool:
    """True iff the preview config asks for the bottom text on the band
    ('Label text' = 'Text over video'). THE one reader of
    preview.text_placement: only the exact string "over_video" counts - any
    other value or type (a hand-edited list, number, null, "OVER_VIDEO",
    " over_video") reads as "caption", the strips. Pure; never raises,
    never writes."""
    try:
        value = cfg.get("text_placement")
    except Exception:
        return False
    return isinstance(value, str) and value == TEXT_PLACEMENT_BAND


def text_band_effective(cfg, *, on_wine, thumb_backend_is_none) -> bool:
    """The layout tiles actually get: wanted (text_band_wanted) AND a Windows
    DWM preview - not Wine / Proton (`on_wine`; the band is a Win32 layered
    window) and no X11 thumbnail helper (`thumb_backend_is_none` False means
    the helper is in use, where a tile refuses the band anyway). Both flags
    must be real bools: anything else reads as the safe answer, the strips.
    Pure: the caller does the Wine probe. Never raises."""
    return (thumb_backend_is_none is True and on_wine is False
            and text_band_wanted(cfg))


@dataclass(frozen=True)
class BandLine:
    """One row of the band, exactly as the tile wants it drawn.

    `text` is ALREADY fitted by the caller (shrunk / ellipsized); `font_px` is
    the glyph height in physical px; `height` the row height in px. `alert`
    marks the decloak banner row: opaque `bg`, where a normal row draws `bg`
    at BAND_BG_ALPHA. Frozen, so a tuple of lines is a comparable mirror key."""
    text: str
    font_px: int
    height: int
    fg: str
    alert: bool = False
    bg: str = "#000000"
    align: str = "w"                # "w" or "center"


@dataclass(frozen=True)
class BandLayout:
    """Band height + each row's (y, h), top-down inside the band."""
    height: int
    rows: tuple


def _int_or(value, default):
    """int(value), or `default` for anything int() refuses (None, a word, NaN,
    inf, a list). Never raises."""
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _shrink_rows(heights, target):
    """Scale `heights` down to sum to `target`, each row staying >= 1.

    Floor division first, then the pixels the floors dropped are handed back
    top-down (so the result is deterministic), then - only when the >= 1 floor
    overshot - taken from the tallest row. Caller guarantees
    len(heights) <= target < sum(heights)."""
    total = sum(heights)
    out = [max(1, h * target // total) for h in heights]
    spare = target - sum(out)
    while spare > 0:
        for i, want in enumerate(heights):
            if spare > 0 and out[i] < want:
                out[i] += 1
                spare -= 1
    while spare < 0:
        tallest = max(range(len(out)), key=out.__getitem__)
        out[tallest] -= 1           # > 1 here: sum(out) > target >= len(out)
        spare += 1
    return out


def _size_or_none(value):
    """A band size in px: a plain int in 1.._MAX_SIZE_PX, else None. A bool,
    a float (even 2.0), a numeric string or an overflowing int is garbage."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 1 <= value <= _MAX_SIZE_PX else None


def layout_band(tile_w, body_h, lines):
    """Where the band's rows go. Pure.

    None when there is nothing to lay out: no lines, a non-iterable `lines`,
    or a tile_w / body_h that is not a plain int in 1.._MAX_SIZE_PX (0, a
    negative, a float, a word, None, 10 ** 400). Otherwise
    BandLayout(height, rows): one (y, h) per line, in the order given,
    stacked top-down with no gap; height == sum of the row heights.

    Each row asks for its line's `height`. When the total exceeds
    int(body_h * 0.60) the rows shrink in proportion - but every row stays
    >= 1, so on a body too small for that the band is one px per row (and the
    floor, not the cap, wins). A garbage row height reads as 1. Never raises:
    anything unforeseen reads as "nothing to lay out"."""
    try:
        tile_w, body_h = _size_or_none(tile_w), _size_or_none(body_h)
        if tile_w is None or body_h is None:
            return None
        lines = tuple(lines or ())
        if not lines:
            return None
        heights = [max(1, _int_or(getattr(line, "height", 1), 1)) for line in lines]
        target = max(int(body_h * _BAND_MAX_FRAC), len(heights))
        if sum(heights) > target:
            heights = _shrink_rows(heights, target)
        rows, y = [], 0
        for h in heights:
            rows.append((y, h))
            y += h
        return BandLayout(height=y, rows=tuple(rows))
    except Exception:
        return None


def _parse_hex(color, fallback):
    """'#rrggbb' (ASCII hex digits only) -> (r, g, b); anything else ->
    `fallback`. Never raises. The digits are checked explicitly because
    int(s, 16) accepts '-1', '+f', ' f' and non-ASCII digits."""
    if (isinstance(color, str) and len(color) == 7 and color[0] == "#"
            and all(c in _HEX_DIGITS for c in color[1:])):
        return (int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16))
    return fallback


def compose_pixels(coverage, fg, bg, bg_alpha):
    """Glyph coverage -> premultiplied BGRA, 4 bytes per coverage byte. Pure.

    The text (colour `fg`, its alpha = the coverage) is laid over the
    background (colour `bg` at `bg_alpha`), so per pixel:
        coverage 0   -> the background at bg_alpha
        coverage 255 -> fg at alpha 255
    with alpha rising linearly between. PREMULTIPLIED: every colour channel is
    already scaled by the pixel's alpha (what UpdateLayeredWindow wants), so a
    channel never exceeds its alpha.

    A garbage colour string falls back (fg white, bg black) and a garbage
    bg_alpha reads as BAND_BG_ALPHA; nothing here raises on bad style input.

    One 256-entry table per channel + bytes.translate, not a Python loop per
    pixel: a 220 x 40 band is 8,800 pixels."""
    coverage = bytes(coverage or b"")
    fr, fgr, fb = _parse_hex(fg, _FG_FALLBACK)
    br, bgr, bb = _parse_hex(bg, _BG_FALLBACK)
    a0 = max(0, min(255, _int_or(bg_alpha, BAND_BG_ALPHA)))

    def channel(f, b):
        # (f * c/255) + (b * a0/255) * (1 - c/255), rounded half-up. The alpha
        # table below is the same expression with f = b = 255, and half-up
        # rounding is monotonic - which is what keeps channel <= alpha.
        return bytes((f * c * 255 + b * a0 * (255 - c) + 32512) // 65025
                     for c in range(256))

    out = bytearray(4 * len(coverage))
    out[0::4] = coverage.translate(channel(fb, bb))
    out[1::4] = coverage.translate(channel(fgr, bgr))
    out[2::4] = coverage.translate(channel(fr, br))
    out[3::4] = coverage.translate(
        bytes((c * 255 + a0 * (255 - c) + 127) // 255 for c in range(256)))
    return bytes(out)


def _glyph_px_max(row_h):
    """The largest font_px whose drawn line fits a row `row_h` px tall
    without losing a px (>= 1). The renderer asks GDI for a GLYPH (em)
    height, but GDI centres and clips the whole CELL, and bold Consolas'
    cell is about 1.171 em - so a glyph as tall as the row, or even 2 px
    shorter, loses the tops / descenders of its letters.

    Measured on a hidden desktop (2026-10-13), rows 3..60 px, five texts
    with ascenders, descenders, brackets and '|': row_h * 2048 // 2398
    never clips; row_h - 2 clipped at rows 19, 23 and every row from 28.
    A row with room to spare is untouched (this is only a ceiling): at
    100 % scaling the label sizes up to 13 pt keep their font, at 150 % up
    to 8 pt; above that the glyph shrinks to fit its row instead of being
    cut. Strictly smaller than the row from row_h = 2 on."""
    return max(1, int(row_h) * _CELL_EM_DEN // _CELL_EM_NUM)


def _alpha255(alpha):
    """Tile opacity 0.0..1.0 -> the band's constant alpha 0..255. The band
    fades WITH its tile (0.85 at rest, 1.0 hovered or active). Garbage reads
    as opaque. Never raises."""
    try:
        return max(0, min(255, int(round(float(alpha) * 255))))
    except (TypeError, ValueError, OverflowError):
        return 255


class BandWindow:
    """One text band for one tile: a small layered popup OWNED by the tile's
    hwnd, covering the bottom of the tile body.

    Tk-thread (= the owner tile's thread) ONLY - every method, destroy()
    included. The real backend refuses a window of another thread, so a
    destroy() called from another thread zeroes `hwnd` but removes nothing:
    the window is orphaned until its owner dies, while `hwnd` reads 0.

    sync() is the ONLY way state reaches the window, and it is ZERO-WRITE: it
    diffs the request against what was last successfully applied and makes
    only the backend calls the difference needs -

        text / style / width / layout changed -> render each row + ONE push
        only the position changed             -> ONE move
        only the opacity changed              -> ONE push of the retained
                                                 pixels (no render)
        shown <-> hidden, or no lines left    -> ONE show / hide
        nothing changed                       -> nothing

    Mirrors latch only AFTER the backend call succeeded (the tile setters'
    rule), so a failed call is simply retried by the next sync. Nothing here
    ever re-stacks the window: it rides its owner (module docstring).

    The window is created lazily, on the first sync that has something to
    show, and only for an owner that is this process's own window. While
    hidden it is left alone - a move or a re-label the tile makes meanwhile
    costs nothing until the band is shown again.

    All Win32 is behind `win32` (default: the lazy _real_band_win32());
    tests inject a recording fake. No GDI object outlives a backend call, so
    there is nothing to leak between syncs and destroy() has only the window
    to remove.

    `last_failure` says why the last sync() answered False, for the tile's
    one log line: None after a sync that succeeded, else a short ASCII string
    naming the step that failed (request / owner / create / render / push /
    move / show) plus the Win32 error code when the backend has one. It
    never holds text from the request (no character name, no label)."""

    def __init__(self, owner_hwnd, win32=None):
        self._owner = _int_or(owner_hwnd, 0)
        self._win32 = win32
        self.hwnd = 0                   # 0 while no window exists
        self.last_failure = None        # see the class docstring
        self._step = "request"          # the step _sync is in (for last_failure)
        self._forget()

    def _forget(self):
        """Reset every mirror: nothing is known to be applied."""
        self._painted = None            # (w, layout, lines) of the pushed bitmap
        self._bgra = b""                # its pixels, kept for an opacity-only push
        self._pos = None                # (x, y) the window was last placed at
        self._alpha = None              # constant alpha (0..255) last pushed
        self._shown = False             # True while the window is shown

    def _backend(self):
        return self._win32 if self._win32 is not None else _real_band_win32()

    def sync(self, x, y, w, body_h, lines, alpha, visible) -> bool:
        """Make the band match the request; see the class docstring for what
        each kind of change costs.

        (x, y) is the top-left of the tile BODY in physical px, `w` the tile
        width and `body_h` the body height; the band covers the bottom
        layout.height px of that body, full width. `lines` is a tuple of
        BandLine (top-down), `alpha` the tile's own opacity.

        True when the window now matches the request - "nothing to show,
        window hidden (or never created)" included. False on any backend
        failure (last_failure says which step); the caller falls back to its
        strips. NEVER raises: this is reached from the preview tick, where an
        escaping exception counts against the whole preview subsystem.

        Blind spot, accepted by design: being zero-write, an UNCHANGED request
        makes no backend call at all, so a window destroyed behind the band's
        back (not by its owner dying with it - that is the tile's own
        teardown) is noticed only on the next CHANGED sync, whose failing call
        finds the hwnd dead and starts over with a fresh window."""
        self._step = "request"
        try:
            ok = self._sync(x, y, w, body_h, lines, alpha, visible)
        except Exception as exc:
            ok = self._failed(exc)
        if ok:
            self.last_failure = None
        return ok

    def _sync(self, x, y, w, body_h, lines, alpha, visible):
        layout = layout_band(w, body_h, lines) if visible else None
        if layout is None:
            return self._hide()
        lines = tuple(lines)            # iterable: layout_band said so
        bx, by = int(x), int(y) + body_h - layout.height
        alpha255 = _alpha255(alpha)
        win32 = self._backend()
        if not self.hwnd and not self._create(win32, bx, by, w, layout.height):
            return self._failed()
        key = (w, layout, lines)
        repaint = key != self._painted
        if repaint or alpha255 != self._alpha:
            self._step = "render"
            bgra = self._render(win32, w, layout, lines) if repaint else self._bgra
            if bgra is None:
                return self._failed()
            # push() also places the window, so it covers a move made together
            # with the repaint / fade - still ONE call.
            self._step = "push"
            if not win32.push(self.hwnd, bx, by, w, layout.height, bgra, alpha255):
                return self._failed()
            self._painted, self._bgra = key, bgra
            self._pos, self._alpha = (bx, by), alpha255
        elif (bx, by) != self._pos:
            self._step = "move"
            if not win32.move(self.hwnd, bx, by):
                return self._failed()
            self._pos = (bx, by)
        if not self._shown:
            self._step = "show"
            if not win32.show(self.hwnd, True):
                return self._failed()
            self._shown = True
        return True

    def _hide(self):
        """Nothing to show: hide the window if one is up, else do nothing."""
        if self.hwnd and self._shown:
            self._step = "show"
            if not self._backend().show(self.hwnd, False):
                return self._failed()
            self._shown = False
        return True

    def _create(self, win32, x, y, w, h):
        """Create the (hidden) window. Refuses an owner that is not this
        process's own window - the compliance gate, asked BEFORE anything is
        created. A garbage owner never reaches the backend at all.

        Where the new window lands in the z-order (measured on a hidden
        desktop, 2026-10-13): at the HEAD of the topmost band - above its
        tile, but also above any neighbour stacked over that tile. It sits
        DIRECTLY above its own tile only from the tile's next re-top on (the
        preview tick's retop batch: at the latest the 2 s heartbeat). Nothing
        here re-stacks it sooner - that would break FPS invariant 1."""
        self._step = "owner"
        if self._owner <= 0 or not win32.is_own_window(self._owner):
            return False
        self._step = "create"
        self.hwnd = _int_or(win32.create(self._owner, x, y, w, h), 0)
        self._forget()
        return bool(self.hwnd)

    def _render(self, win32, w, layout, lines):
        """Every row's pixels, top-down, as one premultiplied BGRA buffer - or
        None when the backend hands back a row of the wrong size."""
        parts = []
        for line, (_y, h) in zip(lines, layout.rows):
            font_px = max(1, min(_int_or(line.font_px, h), _glyph_px_max(h)))
            align = "center" if line.align == "center" else "w"
            text = "" if line.text is None else str(line.text)
            coverage = win32.render_coverage(w, h, text, font_px, align)
            if len(coverage) != w * h:
                return None
            parts.append(compose_pixels(
                coverage, line.fg, line.bg,
                _ALERT_BG_ALPHA if line.alert else BAND_BG_ALPHA))
        return b"".join(parts)

    def _failed(self, exc=None):
        """A step failed (or raised `exc`): record last_failure, then - the
        band is OWNED, so it dies with its tile's window - if the hwnd is
        gone, forget it, and the next sync creates a fresh window instead of
        pushing to a dead handle forever. Returns False (the sync result)."""
        why = self._step
        if exc is not None:
            why += " raised " + type(exc).__name__
        else:
            why += " failed"
        code = 0
        if self._step != "request":     # a backend exists past this step
            try:
                # Read BEFORE the liveness probe below can overwrite it.
                code = int(getattr(self._backend(), "last_error",
                                   lambda: 0)() or 0)
            except Exception:
                code = 0
        if code:
            why += " (win32 error %d)" % code
        if self.hwnd:
            try:
                alive = bool(self._backend().is_window(self.hwnd))
            except Exception:
                alive = True            # cannot tell: keep it for destroy()
            if not alive:
                self.hwnd = 0
                self._forget()
                why += ", window gone"
        # ASCII only, whatever a class name holds (it reaches a shared log).
        self.last_failure = "".join(
            c if " " <= c <= "~" else "?" for c in why)[:120]
        return False

    def destroy(self) -> None:
        """Remove the window. Idempotent, never raises; hwnd == 0 afterwards.
        A later sync() starts over with a new window.

        Tk-thread (owner-thread) only: the real backend removes only a window
        of the calling thread, so from any other thread this zeroes `hwnd`
        and leaves the window orphaned until its owner dies."""
        hwnd, self.hwnd = self.hwnd, 0
        self._forget()
        if hwnd:
            try:
                self._backend().destroy(hwnd)
            except Exception:
                pass                    # owned: the tile's own teardown takes it


class _RealBandWin32:  # pragma: no cover - the one real-Win32 test + live use
    """Real ctypes backend for the band window.

    PRIVATE WinDLL instances with explicit argtypes / restype on every
    function: an undeclared HWND is truncated to 32 bits on Win64 (the trap
    that once made a re-top silently no-op), and prototypes set here must
    never leak into the shared ctypes.windll singletons other wrappers use.

    Every method that acts on a window first checks that the window belongs
    to this process AND this thread (_own). The thread half is not pedantry:
    a popup whose owner lives on another thread makes the system join the
    two threads' input queues behind our back."""

    WS_POPUP = 0x80000000
    WS_EX_LAYERED = 0x00080000          # painted through UpdateLayeredWindow
    WS_EX_TRANSPARENT = 0x00000020      # click-through: input goes to the tile
    WS_EX_NOACTIVATE = 0x08000000       # never the active / focus window
    WS_EX_TOOLWINDOW = 0x00000080       # no taskbar button, not in Alt-Tab
    BAND_EX_STYLE = (WS_EX_LAYERED | WS_EX_TRANSPARENT
                     | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW)
    SW_HIDE = 0
    SW_SHOWNOACTIVATE = 4
    SWP_NOSIZE = 0x0001
    SWP_NOZORDER = 0x0004
    SWP_NOACTIVATE = 0x0010
    ULW_ALPHA = 2
    AC_SRC_OVER = 0
    AC_SRC_ALPHA = 1
    DT_CENTER = 0x0001
    DT_VCENTER = 0x0004
    DT_SINGLELINE = 0x0020
    DT_NOPREFIX = 0x0800                # "&" is a character, not an accelerator
    DT_END_ELLIPSIS = 0x8000
    FW_BOLD = 700
    DEFAULT_CHARSET = 1
    ANTIALIASED_QUALITY = 4             # greyscale AA: R == G == B == coverage
    BK_TRANSPARENT = 1
    WHITE = 0x00FFFFFF

    def __init__(self):
        import ctypes
        from ctypes import wintypes as wt
        self._ctypes = ctypes
        self._wt = wt

        class BLENDFUNCTION(ctypes.Structure):
            _fields_ = [("BlendOp", ctypes.c_ubyte), ("BlendFlags", ctypes.c_ubyte),
                        ("SourceConstantAlpha", ctypes.c_ubyte),
                        ("AlphaFormat", ctypes.c_ubyte)]

        class BITMAPINFOHEADER(ctypes.Structure):
            _fields_ = [("biSize", wt.DWORD), ("biWidth", wt.LONG),
                        ("biHeight", wt.LONG), ("biPlanes", wt.WORD),
                        ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
                        ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", wt.LONG),
                        ("biYPelsPerMeter", wt.LONG), ("biClrUsed", wt.DWORD),
                        ("biClrImportant", wt.DWORD)]

        self._BLENDFUNCTION = BLENDFUNCTION
        self._BITMAPINFOHEADER = BITMAPINFOHEADER

        u = ctypes.WinDLL("user32", use_last_error=True)
        g = ctypes.WinDLL("gdi32", use_last_error=True)
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        # -- the window ------------------------------------------------------
        u.CreateWindowExW.restype = wt.HWND
        u.CreateWindowExW.argtypes = [
            wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD, ctypes.c_int,
            ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.HWND, wt.HMENU,
            wt.HINSTANCE, wt.LPVOID]
        u.DestroyWindow.restype = wt.BOOL
        u.DestroyWindow.argtypes = [wt.HWND]
        u.ShowWindow.restype = wt.BOOL
        u.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
        u.SetWindowPos.restype = wt.BOOL
        u.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_int, wt.UINT]
        u.UpdateLayeredWindow.restype = wt.BOOL
        u.UpdateLayeredWindow.argtypes = [
            wt.HWND, wt.HDC, ctypes.POINTER(wt.POINT), ctypes.POINTER(wt.SIZE),
            wt.HDC, ctypes.POINTER(wt.POINT), wt.COLORREF,
            ctypes.POINTER(BLENDFUNCTION), wt.DWORD]
        # -- read-only probes (ownership, liveness, the show result) ---------
        u.IsWindow.restype = wt.BOOL
        u.IsWindow.argtypes = [wt.HWND]
        u.IsWindowVisible.restype = wt.BOOL
        u.IsWindowVisible.argtypes = [wt.HWND]
        u.GetWindowThreadProcessId.restype = wt.DWORD
        u.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
        k.GetCurrentProcessId.restype = wt.DWORD
        k.GetCurrentProcessId.argtypes = []
        k.GetCurrentThreadId.restype = wt.DWORD
        k.GetCurrentThreadId.argtypes = []
        # -- text into a memory bitmap ---------------------------------------
        u.DrawTextW.restype = ctypes.c_int
        u.DrawTextW.argtypes = [wt.HDC, wt.LPCWSTR, ctypes.c_int,
                                ctypes.POINTER(wt.RECT), wt.UINT]
        g.CreateCompatibleDC.restype = wt.HDC
        g.CreateCompatibleDC.argtypes = [wt.HDC]
        g.CreateDIBSection.restype = wt.HBITMAP
        g.CreateDIBSection.argtypes = [
            wt.HDC, ctypes.POINTER(BITMAPINFOHEADER), wt.UINT,
            ctypes.POINTER(ctypes.c_void_p), wt.HANDLE, wt.DWORD]
        g.SelectObject.restype = wt.HGDIOBJ
        g.SelectObject.argtypes = [wt.HDC, wt.HGDIOBJ]
        g.DeleteObject.restype = wt.BOOL
        g.DeleteObject.argtypes = [wt.HGDIOBJ]
        g.DeleteDC.restype = wt.BOOL
        g.DeleteDC.argtypes = [wt.HDC]
        g.SetTextColor.restype = wt.COLORREF
        g.SetTextColor.argtypes = [wt.HDC, wt.COLORREF]
        g.SetBkMode.restype = ctypes.c_int
        g.SetBkMode.argtypes = [wt.HDC, ctypes.c_int]
        g.CreateFontW.restype = wt.HFONT
        g.CreateFontW.argtypes = [ctypes.c_int] * 5 + [wt.DWORD] * 8 + [wt.LPCWSTR]
        g.GdiFlush.restype = wt.BOOL
        g.GdiFlush.argtypes = []
        self._user32, self._gdi32, self._kernel32 = u, g, k

    # -- ownership / liveness (reads) ----------------------------------------
    def is_own_window(self, hwnd) -> bool:
        """True only for a live window of THIS process created on THIS thread."""
        try:
            hwnd = int(hwnd or 0)
            if hwnd <= 0:
                return False
            pid = self._wt.DWORD(0)
            tid = self._user32.GetWindowThreadProcessId(
                hwnd, self._ctypes.byref(pid))
            return (bool(tid)
                    and pid.value == self._kernel32.GetCurrentProcessId()
                    and tid == self._kernel32.GetCurrentThreadId())
        except Exception:
            return False

    _own = is_own_window

    def is_window(self, hwnd) -> bool:
        return bool(hwnd) and bool(self._user32.IsWindow(hwnd))

    # -- the window ----------------------------------------------------------
    def last_error(self) -> int:
        """The Win32 error code of the last failed call in this backend's
        current method (0 = none recorded). Each acting method clears it on
        entry, so a stale code from an earlier call is never reported."""
        return int(self._ctypes.get_last_error())

    def _clear_error(self):
        self._ctypes.set_last_error(0)

    def create(self, owner_hwnd, x, y, w, h) -> int:
        """A hidden popup owned by `owner_hwnd`; 0 on failure or a foreign
        owner. Class "Static" so no Python window procedure exists. Being
        owned by a topmost tile it is created topmost - at the HEAD of the
        topmost band, NOT necessarily directly above its tile: it gets there
        on the tile's next re-top (see BandWindow._create)."""
        self._clear_error()
        if not self._own(owner_hwnd):
            return 0
        hwnd = self._user32.CreateWindowExW(
            self.BAND_EX_STYLE, "Static", None, self.WS_POPUP,
            int(x), int(y), int(w), int(h), int(owner_hwnd), None, None, None)
        return int(hwnd or 0)

    def push(self, hwnd, x, y, w, h, bgra, alpha255) -> bool:
        """Paint AND place: UpdateLayeredWindow with per-pixel alpha (the
        premultiplied `bgra`, w * h * 4 bytes, top-down) times the constant
        alpha `alpha255`. The window keeps its own copy of the pixels, so the
        DC and bitmap built here are freed before returning. Changes the
        window's position and size, never its place in the z-order."""
        ct, wt, g = self._ctypes, self._wt, self._gdi32
        self._clear_error()
        w, h = int(w), int(h)
        n = w * h * 4
        if w < 1 or h < 1 or len(bgra) != n or not self._own(hwnd):
            return False
        hdr = self._BITMAPINFOHEADER(ct.sizeof(self._BITMAPINFOHEADER),
                                     w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
        hdc = g.CreateCompatibleDC(None)
        if not hdc:
            return False
        hbmp = old_bmp = None
        try:
            bits = ct.c_void_p()
            hbmp = g.CreateDIBSection(hdc, ct.byref(hdr), 0, ct.byref(bits), None, 0)
            if not hbmp or not bits.value:
                return False
            ct.memmove(bits, bytes(bgra), n)
            old_bmp = g.SelectObject(hdc, hbmp)
            blend = self._BLENDFUNCTION(self.AC_SRC_OVER, 0,
                                        max(0, min(255, int(alpha255))),
                                        self.AC_SRC_ALPHA)
            return bool(self._user32.UpdateLayeredWindow(
                hwnd, None, ct.byref(wt.POINT(int(x), int(y))),
                ct.byref(wt.SIZE(w, h)), hdc, ct.byref(wt.POINT(0, 0)), 0,
                ct.byref(blend), self.ULW_ALPHA))
        finally:
            if old_bmp:
                g.SelectObject(hdc, old_bmp)
            if hbmp:
                g.DeleteObject(hbmp)
            g.DeleteDC(hdc)

    def move(self, hwnd, x, y) -> bool:
        """Move only. NOZORDER is THE invariant: the band is never re-stacked
        by our code, it rides its owner."""
        self._clear_error()
        if not self._own(hwnd):
            return False
        return bool(self._user32.SetWindowPos(
            hwnd, None, int(x), int(y), 0, 0,
            self.SWP_NOSIZE | self.SWP_NOZORDER | self.SWP_NOACTIVATE))

    def show(self, hwnd, visible) -> bool:
        """Show without activating, or hide. ShowWindow's return value is the
        PREVIOUS visibility, not success, so the result is read back."""
        self._clear_error()
        if not self._own(hwnd):
            return False
        self._user32.ShowWindow(
            hwnd, self.SW_SHOWNOACTIVATE if visible else self.SW_HIDE)
        return bool(self._user32.IsWindowVisible(hwnd)) == bool(visible)

    def destroy(self, hwnd) -> None:
        if self._own(hwnd):
            self._user32.DestroyWindow(hwnd)

    # -- text ----------------------------------------------------------------
    def render_coverage(self, w, h, text, font_px, align) -> bytes:
        """Glyph coverage of one row: w * h bytes, top-down, 0 = no ink,
        255 = full ink. Bold Consolas `font_px` px tall (the strips' face),
        one line, vertically centred, inset _TEXT_PAD px each side, cut with
        an end-ellipsis if it still does not fit; align "center" centres it.

        GDI draws WHITE text on a zeroed 32-bit bitmap with greyscale
        antialiasing, so any colour channel of the result IS the coverage.
        The DC, the bitmap and the font all live only inside this call and
        are released in the finally - a failure half-way leaks nothing. A
        failed DrawTextW (it returns 0) raises OSError: a blank row must never
        be reported as drawn text."""
        ct, wt, g = self._ctypes, self._wt, self._gdi32
        self._clear_error()
        w, h = int(w), int(h)
        if w < 1 or h < 1:
            return b""
        text = "" if text is None else str(text)
        n = w * h * 4
        hdr = self._BITMAPINFOHEADER(ct.sizeof(self._BITMAPINFOHEADER),
                                     w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
        hdc = g.CreateCompatibleDC(None)
        if not hdc:
            raise OSError("CreateCompatibleDC failed")
        hbmp = old_bmp = font = old_font = None
        try:
            bits = ct.c_void_p()
            hbmp = g.CreateDIBSection(hdc, ct.byref(hdr), 0, ct.byref(bits), None, 0)
            if not hbmp or not bits.value:
                raise OSError("CreateDIBSection failed")
            old_bmp = g.SelectObject(hdc, hbmp)
            ct.memset(bits, 0, n)
            # a negative height asks for that GLYPH height (not the cell)
            font = g.CreateFontW(-max(1, int(font_px)), 0, 0, 0, self.FW_BOLD,
                                 0, 0, 0, self.DEFAULT_CHARSET, 0, 0,
                                 self.ANTIALIASED_QUALITY, 0, "Consolas")
            if font:
                old_font = g.SelectObject(hdc, font)
            g.SetBkMode(hdc, self.BK_TRANSPARENT)
            g.SetTextColor(hdc, self.WHITE)
            if text:
                pad = min(_TEXT_PAD, (w - 1) // 2)
                rc = wt.RECT(pad, 0, w - pad, h)
                flags = (self.DT_VCENTER | self.DT_SINGLELINE | self.DT_NOPREFIX
                         | self.DT_END_ELLIPSIS
                         | (self.DT_CENTER if align == "center" else 0))
                if not self._user32.DrawTextW(hdc, text, -1, ct.byref(rc), flags):
                    raise OSError("DrawTextW failed")
            g.GdiFlush()
            return ct.string_at(bits, n)[2::4]
        finally:
            if old_font:
                g.SelectObject(hdc, old_font)
            if font:
                g.DeleteObject(font)
            if old_bmp:
                g.SelectObject(hdc, old_bmp)
            if hbmp:
                g.DeleteObject(hbmp)
            g.DeleteDC(hdc)


_REAL_BAND_WIN32 = None


def _real_band_win32():  # pragma: no cover
    """The process-wide real backend, built on first use - never at import
    (the Linux / Wine build imports the preview modules too)."""
    global _REAL_BAND_WIN32
    if _REAL_BAND_WIN32 is None:
        _REAL_BAND_WIN32 = _RealBandWin32()
    return _REAL_BAND_WIN32
