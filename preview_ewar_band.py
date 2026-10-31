"""The EWAR ICON POPUP a preview tile can draw over the TOP of its video.

The sibling of preview_band.py, and deliberately built to the same discipline -
read that module's docstring first: it explains why a second window exists at
all. The short of it: the compositor draws a DWM thumbnail OVER its destination
window's own content, so a child widget inside the thumbnail rectangle is
invisible (one was built and deleted in September 2026). Anything drawn on the
picture has to come from a separate top-level window, OWNED by the tile's hwnd.
A desktop-wide topmost overlay was also tried and retired in July 2026: re-
topping it every tick destroyed in-game FPS.

Where this one sits: the icon row occupies the TOP-LEFT corner of the tile
BODY, inset BODY_INSET px, sized to the row (owner decision D1). The text band
covers the BOTTOM of the same body, so the two do not overlap, and THAT is what
lets BOTH ride their owner's z-order with no ordering call ever made between
them (spec 10.2).

THE NON-OVERLAP IS CONDITIONAL ON A SINGLE-ROW BAND, and that condition is not
enforced anywhere - it is a property of what FCTool ships. Since v6.9.7 the
location strip is fed "" and the band is therefore ONE row, which clears the
icon row at every size and body height. A TWO-row band brings them back into
contact: at Tk scaling 1.5 or more with 27 px icons, tile bodies of roughly 68
to 79 px overlap by 2 to 6 px (the band grows upward from the bottom until the
60 % body cap stops it, while the icon row is a fixed 34 px from the top).
Whoever re-enables the location strip owns that: either keep the icon row and
the band out of each other's way by height, or accept the overlap deliberately
- do NOT reach for a re-stack, which would break invariant 1.

THE THREE INVARIANTS (a change that breaks any of them is a regression):
  1. the popup is never re-stacked by our code - the one window-position call
     in this file carries SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE, and the
     show is SW_SHOWNOACTIVATE. This is the FPS invariant;
  2. an unchanged sync() makes ZERO backend calls - the 250 ms preview tick
     costs a popup nothing. The icons are STATIC (D2) precisely so that this
     is reachable: the set changes on the order of seconds, so the window
     repaints on the order of seconds;
  3. sync() NEVER raises. It is reached from the preview tick, where an
     escaping exception retires the tile and five failed ticks disable every
     preview. On failure it answers False and records `last_failure`.

Layers, top to bottom of the file:
  * pure functions - ewar_row_size (how big the row is) and compose_icons
    (the chips, borders and glyphs as premultiplied BGRA). No window, no
    ctypes; importable anywhere, Wine included.
  * EwarWindow - one popup for one tile, every Win32 call behind an injectable
    backend (win32=), the preview_band / preview_tile house pattern.
  * _RealEwarWin32 - the ctypes backend, built LAZILY by _real_ewar_win32().
    Nothing in this module touches ctypes at import time.

No Tk import and no import of preview_tile or preview_band: the tile imports
this, never the reverse. The handful of private sizing guards below are kept
here rather than borrowed from preview_band so that neither sibling's import
surface or Win32 census depends on the other's.

The ARTWORK comes from ewar_icons.icon_rgba as straight RGBA glyphs; the dark
chip and the red hairline round it are composed HERE (design A4b), so swapping
or withdrawing the art stays an asset edit.

Compliance: every call here targets a window THIS process created on THIS
thread. The owner hwnd is checked before a popup is created. No message, input
or focus function is bound, and nothing draws text (tests pin the census).
"""
from __future__ import annotations

from ewar_icons import ICON_SIZES, icon_rgba

#: The red hairline round each chip (owner decision D2). THE one place this hex
#: is written.
BORDER_COLOR = "#ff3b30"

#: The chip behind a glyph: black at ~72 % alpha (D2). 0.72 * 255 = 183.6.
CHIP_BG_ALPHA = 184

#: Border thickness in px, which is also the glyph's inset inside its chip: a
#: chip is therefore (icon_px + 2) square and the artwork is never scaled.
CHIP_PAD = 1

#: Transparent gap between two chips, in px.
ICON_GAP = 3

#: Inset of the row from the tile body's top-left corner, in px. Public
#: because the TILE needs it to know the popup's footprint.
BODY_INSET = 5

#: A tile width larger than this is garbage - no monitor is that wide, and the
#: arithmetic below would otherwise run on 10 ** 400.
_MAX_SIZE_PX = 1 << 20

#: The ONLY characters a '#rrggbb' colour may hold after the '#'. Explicit and
#: ASCII: int(s, 16) also takes a sign, spaces, underscores and non-ASCII
#: digits.
_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")

_BORDER_FALLBACK = (255, 0, 0)          # an edited-to-garbage border draws red


def _parse_hex(color, fallback):
    """'#rrggbb' (ASCII hex digits only) -> (r, g, b); anything else ->
    `fallback`. Never raises. The digits are checked explicitly because
    int(s, 16) accepts '-1', '+f', ' f' and non-ASCII digits."""
    if (isinstance(color, str) and len(color) == 7 and color[0] == "#"
            and all(c in _HEX_DIGITS for c in color[1:])):
        return (int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16))
    return fallback


_BORDER_RGB = _parse_hex(BORDER_COLOR, _BORDER_FALLBACK)


def _int_or(value, default):
    """int(value), or `default` for anything int() refuses (None, a word, NaN,
    inf, a list). Never raises."""
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _size_or_none(value):
    """A tile width in px: a plain int in 1.._MAX_SIZE_PX, else None. A bool,
    a float (even 220.0), a numeric string or an overflowing int is garbage."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 1 <= value <= _MAX_SIZE_PX else None


def _empty_size(value):
    """True for a plain int below 1: a width that means "no area", not garbage
    (a bool or a float is garbage, see _size_or_none)."""
    return (isinstance(value, int) and not isinstance(value, bool)
            and value < 1)


def _alpha255(alpha):
    """Tile opacity 0.0..1.0 -> the popup's constant alpha 0..255. The icons
    fade WITH the tile (0.85 at rest, 1.0 hovered or active). Garbage reads as
    opaque. Never raises."""
    try:
        return max(0, min(255, int(round(float(alpha) * 255))))
    except (TypeError, ValueError, OverflowError):
        return 255


def _drawable(icons, icon_px):
    """((effect, rgba), ...) for the icons the bundled sheet can actually serve
    at `icon_px`, in the order given, each effect at most once. Pure; never
    raises; () for anything unusable.

    This is the ONE filter: an effect the sheet does not carry, a size that was
    never pre-rendered, a non-iterable, a garbage member, or a sheet that
    failed to load at all (ewar_icons answers None for every call then) simply
    contributes no icon. So an unreadable sheet makes the row empty, and
    EwarWindow hides instead of drawing blank chips - missing icons are a
    cosmetic loss, a dead tile is not.

    `icon_px` must be a plain int that the sheet was built for: 18.0 would
    otherwise hash equal to 18 and quietly match a cell."""
    if isinstance(icon_px, bool) or not isinstance(icon_px, int):
        return ()
    if icon_px not in ICON_SIZES:
        return ()
    try:
        items = tuple(icons or ())
    except TypeError:
        return ()
    out, seen = [], set()
    for effect in items:
        rgba = icon_rgba(effect, icon_px)
        if rgba is None or len(rgba) != icon_px * icon_px * 4:
            continue
        if effect in seen:              # hashable: the lookup above succeeded
            continue
        seen.add(effect)
        out.append((effect, rgba))
    return tuple(out)


def _chip_size(icon_px):
    return icon_px + 2 * CHIP_PAD


def ewar_row_size(icons, icon_px):
    """(width, height) of the icon row in px, or (0, 0) when there is nothing
    to draw. Pure; never raises.

    One chip per drawable icon (_drawable), each (icon_px + 2 * CHIP_PAD)
    square, ICON_GAP px apart. (0, 0) for no icons, an unknown effect, a size
    the sheet does not carry, or any garbage - the caller then shows nothing."""
    cells = _drawable(icons, icon_px)
    if not cells:
        return (0, 0)
    chip = _chip_size(icon_px)
    return (len(cells) * chip + (len(cells) - 1) * ICON_GAP, chip)


def _fit_width(icons, icon_px, tile_w):
    """The LEADING icons whose row fits inside `tile_w` with BODY_INSET px of
    margin each side, as a tuple of effect names. () when not even one fits.

    Five simultaneous icons is the ceiling (spec A3), and spec A3's arithmetic
    holds at 14 px: 92 px of row plus both insets inside a 160 px tile. It does
    NOT hold at every size. Measured against the project's 120 px minimum tile
    width, where the room is 110 px: all five fit at 14 px, FOUR of five at
    18 and 21 px, THREE of five at 27 px. So dropping icons is an ordinary
    outcome on a narrow tile at Tk scaling 1.5 or more, not an unforeseen one.

    It is the TRAILING icons that go, which is why truncating the end is the
    right rule rather than a shrug: the tuple arrives in ICON_ORDER (spec A2),
    which is ordered by how much the effect constrains the pilot, so the first
    icon dropped is nosferatu - the least constraining - and scramble, the most
    constraining, is the last thing that could ever be dropped."""
    chip = _chip_size(icon_px)
    room = tile_w - 2 * BODY_INSET
    if room < chip:
        return ()
    fits = 1 + max(0, (room - chip) // (chip + ICON_GAP))
    return tuple(icons[:fits])


def _chip_pixels(effect, icon_px):
    """ONE chip's premultiplied BGRA, top-down: the border colour on all four
    edges at full alpha, the dark chip inside it, and the glyph composited
    over that. chip * chip * 4 bytes, or b"" when the sheet cannot serve it.

    Per interior pixel, with the glyph's straight (r, g, b, a):
        alpha  = a + CHIP_BG_ALPHA * (255 - a) / 255
        colour = c * a / 255, rounded half-up
    i.e. the glyph over black-at-CHIP_BG_ALPHA, where the black contributes no
    colour at all. PREMULTIPLIED, which is what UpdateLayeredWindow wants, and
    every channel stays <= its alpha: colour <= a <= alpha."""
    rgba = icon_rgba(effect, icon_px)
    if rgba is None or len(rgba) != icon_px * icon_px * 4:
        return b""
    chip = _chip_size(icon_px)
    r, g, b = _BORDER_RGB
    buf = bytearray(bytes((b, g, r, 255)) * (chip * chip))
    span = 4 * icon_px
    for y in range(icon_px):
        row = bytearray(span)
        src = y * span
        for i in range(0, span, 4):
            gr, gg, gb, ga = rgba[src + i:src + i + 4]
            row[i] = (gb * ga + 127) // 255
            row[i + 1] = (gg * ga + 127) // 255
            row[i + 2] = (gr * ga + 127) // 255
            row[i + 3] = ga + (CHIP_BG_ALPHA * (255 - ga) + 127) // 255
        at = 4 * ((y + CHIP_PAD) * chip + CHIP_PAD)
        buf[at:at + span] = row
    return bytes(buf)


#: Composed chips, keyed (effect, icon_px). The sheet carries five effects at
#: four sizes, so this is bounded at 20 entries and 40,520 bytes (39.6 KB) -
#: measured; five effects times the sum of (px + 2) ** 2 * 4 over ICON_SIZES.
#: Every value is deterministic: a chip is the artwork plus two constants.
#: Filled on demand rather than at import, so a tile that never shows an icon,
#: and the Wine build that cannot show one at all, pay nothing.
_CHIPS = {}


def _chip(effect, icon_px):
    key = (effect, icon_px)
    chip = _CHIPS.get(key)
    if chip is None:
        chip = _CHIPS[key] = _chip_pixels(effect, icon_px)
    return chip


def compose_icons(icons, icon_px):
    """The whole icon row as premultiplied BGRA, top-down, exactly
    ewar_row_size(icons, icon_px) pixels - or b"" when that size is (0, 0).
    Pure; never raises.

    Each drawable icon is one chip (_chip_pixels); the ICON_GAP px between two
    chips stay fully transparent zero bytes, so only the chips are ever drawn
    and the row reads as separate badges rather than one bar."""
    cells = _drawable(icons, icon_px)
    if not cells:
        return b""
    chip = _chip_size(icon_px)
    width = len(cells) * chip + (len(cells) - 1) * ICON_GAP
    stride = 4 * width
    out = bytearray(stride * chip)
    left = 0
    for effect, _rgba in cells:
        part = _chip(effect, icon_px)
        if len(part) != 4 * chip * chip:
            return b""                  # the sheet changed under us: draw none
        for y in range(chip):
            at = y * stride + 4 * left
            src = y * 4 * chip
            out[at:at + 4 * chip] = part[src:src + 4 * chip]
        left += chip + ICON_GAP
    return bytes(out)


class EwarWindow:
    """One EWAR icon row for one tile: a small layered popup OWNED by the
    tile's hwnd, covering the top-left corner of the tile body.

    Tk-thread (= the owner tile's thread) ONLY - every method, destroy()
    included. The real backend refuses a window of another thread, so a
    destroy() called from another thread zeroes `hwnd` but removes nothing:
    the window is orphaned until its owner dies, while `hwnd` reads 0.

    sync() is the ONLY way state reaches the window, and it is ZERO-WRITE: it
    diffs the request against what was last successfully applied and makes
    only the backend calls the difference needs -

        icons / icon size / row size changed -> compose + ONE push
        only the position changed            -> ONE move
        only the opacity changed             -> ONE push of the retained
                                                pixels (no compose)
        shown <-> hidden, or no icons left   -> ONE show / hide
        nothing changed                      -> nothing

    The mirror key holds the icon tuple, the icon size AND the row rectangle,
    so a tile moving to a 150 % monitor - same icons, bigger icon_px - repaints
    instead of keeping stale pixels.

    Mirrors latch only AFTER the backend call succeeded (the tile setters'
    rule), so a failed call is simply retried by the next sync. Nothing here
    ever re-stacks the window: it rides its owner (module docstring).

    The window is created lazily, on the first sync that has something to show,
    and only for an owner that is this process's own window. While hidden it is
    left alone - a move the tile makes meanwhile costs nothing until the icons
    are shown again.

    All Win32 is behind `win32` (default: the lazy _real_ewar_win32()); tests
    inject a recording fake. No GDI object outlives a backend call, so there is
    nothing to leak between syncs and destroy() has only the window to remove.

    `last_failure` says why the last sync() answered False, for the tile's one
    log line: None after a sync that succeeded, else a short ASCII string
    naming the step that failed (request / owner / create / render / push /
    move / show) plus the Win32 error code when the backend has one. It never
    holds request content - no effect name, and above all no character name."""

    def __init__(self, owner_hwnd, win32=None):
        self._owner = _int_or(owner_hwnd, 0)
        self._win32 = win32
        self.hwnd = 0                   # 0 while no window exists
        self.last_failure = None        # see the class docstring
        self._step = "request"          # the step _sync is in (for last_failure)
        self._forget()

    def _forget(self):
        """Reset every mirror: nothing is known to be applied."""
        self._painted = None            # (icons, icon_px, w, h) of the bitmap
        self._bgra = b""                # its pixels, kept for an opacity push
        self._pos = None                # (x, y) the window was last placed at
        self._alpha = None              # constant alpha (0..255) last pushed
        self._shown = False             # True while the window is shown

    def _backend(self):
        return self._win32 if self._win32 is not None else _real_ewar_win32()

    def sync(self, x, y, w, icons, icon_px, alpha, visible) -> bool:
        """Make the popup match the request; see the class docstring for what
        each kind of change costs.

        (x, y) is the top-left of the tile BODY in physical px and `w` the tile
        width; the row is placed BODY_INSET px in from that corner and is only
        as large as its icons. `icons` is a tuple of effect names in the order
        they should read (the tracker hands them over in ICON_ORDER), `icon_px`
        the pre-rendered artwork size the tile picked for its own width and Tk
        scaling, and `alpha` the tile's own opacity.

        True when the window now matches the request - "nothing to show, window
        hidden (or never created)" included: not visible, no drawable icon, a
        size the sheet does not carry, a zero / negative tile width, or a tile
        too narrow for even one chip. False on any backend failure, and on
        drawable icons with a tile width this module refuses (a float, a bool,
        an out-of-range int) or a position int() will not take - never True
        while drawing nothing (last_failure says which step). NEVER raises:
        this is reached from the preview tick, where an escaping exception
        counts against the whole preview subsystem.

        On False, EwarWindow itself only records why, and forgets its hwnd if
        the window is gone (so a later sync would create a fresh one). The TILE
        treats any False as a fault: preview_tile latches that tile's icons off
        for the rest of the session and never syncs it again.

        Blind spot, accepted by design: being zero-write, an UNCHANGED request
        makes no backend call at all, so a window destroyed behind the popup's
        back (not by its owner dying with it - that is the tile's own teardown)
        is noticed only on the next CHANGED sync, whose failing call finds the
        hwnd dead - and that False latches the tile's icons off."""
        self._step = "request"
        try:
            ok = self._sync(x, y, w, icons, icon_px, alpha, visible)
        except Exception as exc:
            ok = self._failed(exc)
        if ok:
            self.last_failure = None
        return ok

    def _sync(self, x, y, w, icons, icon_px, alpha, visible):
        if not visible:
            return self._hide()
        wanted = tuple(effect for effect, _rgba in _drawable(icons, icon_px))
        if not wanted:
            return self._hide()         # nothing the sheet can draw
        tile_w = _size_or_none(w)
        if tile_w is None:
            if _empty_size(w):
                return self._hide()     # a tile with no area: nothing to show
            return self._failed()       # step "request": a width it refuses
        kept = _fit_width(wanted, icon_px, tile_w)
        if not kept:
            return self._hide()         # too narrow for one chip
        row_w, row_h = ewar_row_size(kept, icon_px)
        bx, by = int(x) + BODY_INSET, int(y) + BODY_INSET
        alpha255 = _alpha255(alpha)
        win32 = self._backend()
        if not self.hwnd and not self._create(win32, bx, by, row_w, row_h):
            return self._failed()
        key = (kept, icon_px, row_w, row_h)
        repaint = key != self._painted
        if repaint or alpha255 != self._alpha:
            self._step = "render"
            bgra = compose_icons(kept, icon_px) if repaint else self._bgra
            if len(bgra) != 4 * row_w * row_h:
                return self._failed()
            # push() also places and sizes the window, so it covers a move made
            # together with the repaint / fade - still ONE call.
            self._step = "push"
            if not win32.push(self.hwnd, bx, by, row_w, row_h, bgra, alpha255):
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
        process's own window ON THIS THREAD - the compliance gate, asked BEFORE
        anything is created (a popup whose owner lives on another thread makes
        the system join the two threads' input queues behind our back). A
        garbage owner never reaches the backend at all.

        Where a NEW window lands in the z-order, measured for the band on a
        hidden desktop 2026-10-13 and unchanged here: at the HEAD of the
        topmost band - above its tile, but also above any neighbour stacked
        over that tile. It sits DIRECTLY above its own tile from the tile's
        next re-top on (the preview tick's retop batch: at the latest the 2 s
        heartbeat). Nothing here re-stacks it sooner - that would break
        invariant 1."""
        self._step = "owner"
        if self._owner <= 0 or not win32.is_own_window(self._owner):
            return False
        self._step = "create"
        self.hwnd = _int_or(win32.create(self._owner, x, y, w, h), 0)
        self._forget()
        return bool(self.hwnd)

    def _failed(self, exc=None):
        """A step failed (or raised `exc`): record last_failure, then - the
        popup is OWNED, so it dies with its tile's window - if the hwnd is
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
        of the calling thread, so from any other thread this zeroes `hwnd` and
        leaves the window orphaned until its owner dies."""
        hwnd, self.hwnd = self.hwnd, 0
        self._forget()
        if hwnd:
            try:
                self._backend().destroy(hwnd)
            except Exception:
                pass                    # owned: the tile's own teardown takes it


class _RealEwarWin32:  # pragma: no cover - the one real-Win32 test + live use
    """Real ctypes backend for the EWAR icon popup.

    PRIVATE WinDLL instances with explicit argtypes / restype on every
    function: an undeclared HWND is truncated to 32 bits on Win64 (the trap
    that once made a re-top silently no-op), and prototypes set here must never
    leak into the shared ctypes.windll singletons other wrappers use.

    Every method that acts on a window first checks that the window belongs to
    this process AND this thread (_own).

    Note what is NOT here: no font, no text, no read-back of GDI-drawn bits.
    The chips are composed in Python (compose_icons) and this backend only
    hands the finished buffer to the compositor. So this census is FIFTEEN
    names: the band's twenty, minus the five it needs for text."""

    WS_POPUP = 0x80000000
    WS_EX_LAYERED = 0x00080000          # painted through UpdateLayeredWindow
    WS_EX_TRANSPARENT = 0x00000020      # click-through: input goes to the tile
    WS_EX_NOACTIVATE = 0x08000000       # never the active / focus window
    WS_EX_TOOLWINDOW = 0x00000080       # no taskbar button, not in Alt-Tab
    EWAR_EX_STYLE = (WS_EX_LAYERED | WS_EX_TRANSPARENT
                     | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW)
    SW_HIDE = 0
    SW_SHOWNOACTIVATE = 4
    SWP_NOSIZE = 0x0001
    SWP_NOZORDER = 0x0004
    SWP_NOACTIVATE = 0x0010
    ULW_ALPHA = 2
    AC_SRC_OVER = 0
    AC_SRC_ALPHA = 1

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
        # -- the premultiplied DIB the pixels are pushed through -------------
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
        self._user32, self._gdi32, self._kernel32 = u, g, k

    # -- ownership / liveness (reads) ----------------------------------------
    def is_own_window(self, hwnd) -> bool:
        """True only for a live window of THIS process created on THIS thread.
        Clears the last-error code first, like the acting methods: its answer
        is EwarWindow's "owner" step, whose failure reports last_error()."""
        try:
            self._clear_error()
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
        current method (0 = none recorded). Each acting method - and the
        ownership probe is_own_window - clears it on entry, so a stale code
        from an earlier call is never reported."""
        return int(self._ctypes.get_last_error())

    def _clear_error(self):
        self._ctypes.set_last_error(0)

    def create(self, owner_hwnd, x, y, w, h) -> int:
        """A hidden popup owned by `owner_hwnd`; 0 on failure or a foreign
        owner. Class "Static" so no Python window procedure exists. Being
        owned by a topmost tile it is created topmost - at the HEAD of the
        topmost band, NOT necessarily directly above its tile: it gets there
        on the tile's next re-top (see EwarWindow._create)."""
        self._clear_error()
        if not self._own(owner_hwnd):
            return 0
        hwnd = self._user32.CreateWindowExW(
            self.EWAR_EX_STYLE, "Static", None, self.WS_POPUP,
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
        """Move only. NOZORDER is THE invariant: the popup is never re-stacked
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


_REAL_EWAR_WIN32 = None


def _real_ewar_win32():  # pragma: no cover
    """The process-wide real backend, built on first use - never at import
    (the Linux / Wine build imports the preview modules too)."""
    global _REAL_EWAR_WIN32
    if _REAL_EWAR_WIN32 is None:
        _REAL_EWAR_WIN32 = _RealEwarWin32()
    return _REAL_EWAR_WIN32
