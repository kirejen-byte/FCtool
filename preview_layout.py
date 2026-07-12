"""Pure geometry/cycling math for native previews. No Tk, no ctypes — fully unit-tested.
All rects are (x, y, w, h) in physical px, virtual-screen coordinate space."""
from __future__ import annotations

EDGE_SNAP_MIN = 20          # EVE-O parity: max(20, w // 10)
LOGIN_STACK_STEP = 24
CLAMP_MIN_VISIBLE_PX = 40   # clamp_visible: min on-desktop overlap (both axes) to leave a rect alone


def clamp_rect(rect, bounds):
    x, y, w, h = rect
    bx, by, bw, bh = bounds
    x = max(bx, min(x, bx + bw - w))
    y = max(by, min(y, by + bh - h))
    return (x, y, w, h)


def clamp_visible(x, y, w, h, bounds, min_px=CLAMP_MIN_VISIBLE_PX):
    """Clamp a RESTORED tile position onto `bounds` only when it isn't usably
    visible there — the stranded-monitor guard for saved preview layouts.
    `bounds` is (bx, by, bw, bh), the same x/y/width/height convention as
    clamp_rect/grid_arrange (NOT the (x0, y0, x1, y1) edges that the win32
    _virtual_screen_bounds() hook itself returns — callers must convert).

    A rect is left UNCHANGED as long as its intersection with bounds is at
    least `min_px` wide AND `min_px` tall: a multi-monitor user with a
    temporarily-off display must not have a still-grabbable tile silently
    rearranged. Anything less (fully offscreen, or a sliver under min_px on
    either axis) is fully clamped via clamp_rect's formula, which pins a
    tile wider/taller than bounds to the bounds' own (bx, by) origin.

    Returns (x, y) only — w/h never change here."""
    bx, by, bw, bh = bounds
    ix = max(0, min(x + w, bx + bw) - max(x, bx))
    iy = max(0, min(y + h, by + bh) - max(y, by))
    if ix >= min_px and iy >= min_px:
        return (x, y)
    cx, cy, _, _ = clamp_rect((x, y, w, h), bounds)
    return (cx, cy)


def snap_to_grid(x, y, grid_w, grid_h):
    return (round(x / grid_w) * grid_w, round(y / grid_h) * grid_h)


def snap_to_edges(rect, others):
    x, y, w, h = rect
    thr = max(EDGE_SNAP_MIN, w // 10)
    for ox, oy, ow, oh in others:
        candidates_x = [ox + ow, ox - w, ox]          # right-edge, left-edge, align-left
        candidates_y = [oy, oy + oh, oy - h]          # align-top, below, above
        for cx in candidates_x:
            if abs(x - cx) <= thr and _overlaps(y, h, oy, oh):
                x = cx
                for cy in (oy, oy + oh - h):
                    if abs(y - cy) <= thr:
                        y = cy
                        break
                return (x, y)
    return (x, y)


def _overlaps(a, alen, b, blen):
    return a < b + blen and b < a + alen


def grid_arrange(count, tile_w, tile_h, bounds, origin=(10, 10), gap=8):
    bx, by, bw, bh = bounds
    ox, oy = origin
    per_row = max(1, (bw - ox) // (tile_w + gap))
    out = []
    for i in range(count):
        row, col = divmod(i, per_row)
        out.append((ox + col * (tile_w + gap), oy + row * (tile_h + gap),
                    tile_w, tile_h))
    return out


def login_stack_pos(index, base):
    return (base[0] + index * LOGIN_STACK_STEP, base[1] + index * LOGIN_STACK_STEP)


_ZOOM_ANCHORS = {
    # anchor -> (fx, fy) fraction of the size DELTA to subtract from x / y.
    # 0.0 keeps that edge fixed, 1.0 keeps the opposite edge fixed, 0.5 centers.
    "nw": (0.0, 0.0), "n": (0.5, 0.0), "ne": (1.0, 0.0),
    "w":  (0.0, 0.5), "c": (0.5, 0.5), "e":  (1.0, 0.5),
    "sw": (0.0, 1.0), "s": (0.5, 1.0), "se": (1.0, 1.0),
}


def zoom_rect(rect, factor, anchor):
    """Scale (x, y, w, h) by factor around one of 9 anchors (nw n ne w c e sw s se).

    The anchor point stays fixed; the rect grows away from it. factor <= 1 is a
    no-op. Unknown anchors fall back to 'nw'. Returns integer-rounded (x, y, w, h).
    """
    x, y, w, h = rect
    if factor <= 1:
        return (x, y, w, h)
    nw = max(1, round(w * factor))
    nh = max(1, round(h * factor))
    fx, fy = _ZOOM_ANCHORS.get(anchor, _ZOOM_ANCHORS["nw"])
    nx = round(x - (nw - w) * fx)
    ny = round(y - (nh - h) * fy)
    return (nx, ny, nw, nh)


def cycle_next(order, current, live, direction, strict=False):
    """Next live char key in the ordered ring. Empty order → sorted(live).

    strict=True → members-only ring: `[k for k in order if k in live]` with NO
    extras-append (non-member live clients are never cycled) and direction-aware
    entry when the anchor is outside the ring — forward starts at ring[0],
    backward at ring[-1]. An empty ring (no live members, or empty `order`)
    returns None. strict=False (the default) keeps the legacy cycle-all path
    byte-for-byte.
    """
    if strict:
        ring = [k for k in order if k in live]
        if not ring:
            return None
        if current not in ring:
            return ring[0] if direction > 0 else ring[-1]
        i = ring.index(current)
        return ring[(i + direction) % len(ring)]
    ring = [k for k in order if k in live] if order else sorted(live)
    if not ring:
        return None
    extras = [k for k in sorted(live) if k not in ring]
    ring += extras
    if current not in ring:
        return ring[0]
    i = ring.index(current)
    return ring[(i + direction) % len(ring)]
