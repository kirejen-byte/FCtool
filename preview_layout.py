"""Pure geometry/cycling math for native previews. No Tk, no ctypes — fully unit-tested.
All rects are (x, y, w, h) in physical px, virtual-screen coordinate space."""
from __future__ import annotations

EDGE_SNAP_MIN = 20          # EVE-O parity: max(20, w // 10)
LOGIN_STACK_STEP = 24


def clamp_rect(rect, bounds):
    x, y, w, h = rect
    bx, by, bw, bh = bounds
    x = max(bx, min(x, bx + bw - w))
    y = max(by, min(y, by + bh - h))
    return (x, y, w, h)


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


def cycle_next(order, current, live, direction):
    """Next live char key in the ordered ring. Empty order → sorted(live)."""
    ring = [k for k in order if k in live] if order else sorted(live)
    if not ring:
        return None
    extras = [k for k in sorted(live) if k not in ring]
    ring += extras
    if current not in ring:
        return ring[0]
    i = ring.index(current)
    return ring[(i + direction) % len(ring)]
