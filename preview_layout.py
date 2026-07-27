"""Pure geometry/cycling math for native previews. No Tk, no ctypes — fully unit-tested.
All rects are (x, y, w, h) in physical px, virtual-screen coordinate space."""
from __future__ import annotations

EDGE_SNAP_MIN = 20          # EVE-O parity: max(20, w // 10)
LOGIN_STACK_STEP = 24
CLAMP_MIN_VISIBLE_PX = 40   # clamp_visible: min on-desktop overlap (both axes) to leave a rect alone
SNAP_THRESHOLD_PX = 12      # snap_rect: max px gap at which an edge sticks to a neighbour's edge
# clamp_size: the tile-size floor. THE single definition for the whole FCPreview
# subsystem — preview_tile's drag paths and every fc_gui controller path both
# read it from here. It used to live only in preview_tile, so the controller
# (settings spinbox, per-char size map, saved layout rects) could drive a tile
# to 0 px while the drag paths were correctly floored.
MIN_TILE_W = 120            # min tile WIDTH in physical px
MIN_TILE_BODY_H = 68        # min tile BODY height in physical px (strip is extra)


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


def _floor_int(value, floor):
    """`value` as an int, never below `floor`. Junk — None, "", NaN, inf, a
    non-numeric string — resolves to `floor` rather than raising: this runs on
    the config-load path, where a hand-edited or foreign value is exactly what
    is being defended against. Shared by clamp_size and heal_preview_sizes so
    they cannot drift apart."""
    try:
        v = int(value)
    except (TypeError, ValueError, OverflowError):
        return floor
    return floor if v < floor else v


def clamp_size(w, body_h, min_w=MIN_TILE_W, min_body_h=MIN_TILE_BODY_H):
    """Floor a tile's (w, body_h) so it can never be shrunk into invisibility —
    the SIZE companion to clamp_visible's POSITION guard.

    `body_h` EXCLUDES the caption strip (preview_tile.STRIP_H), like every other
    body_h in the subsystem, so the floor is a 120 x (68 + 20) = 120x88 window.
    At that size the caption strip still renders ~11 glyphs of the pilot name
    (120 px minus 39 px of dot/chip/exclusion reserve = 81 px, at ~7 px/glyph
    for Consolas 9 bold) and leaves a 96 x 7 px band of strip outside the two
    12 px corner-resize zones to left-drag by — right-drag-to-move works
    anywhere on the tile — so a floored tile stays both identifiable and
    grabbable. The whole window is also >= 2x clamp_visible's 40 px
    "usably visible" threshold on both axes.

    Idempotent: clamping an already-clamped pair is a no-op, so a value that
    passes through two clamped paths is not clamped twice into something else.

    Returns (w, body_h) as ints."""
    return _floor_int(w, min_w), _floor_int(body_h, min_body_h)


def heal_preview_sizes(cfg):
    """Floor every PERSISTED tile size in a preview config dict, IN PLACE.

    The self-heal companion to clamp_size, mirroring how clamp_visible's callers
    rescue a stranded saved POSITION: a user whose config already carries a
    sub-minimum size — hand-edited, or written by a build that did not enforce
    the floor on the settings/import paths — must be rescued on next launch, not
    left with an invisible tile they cannot grab in order to fix it.

    Three size-bearing shapes, all optional:
        cfg['tile_w'] / cfg['tile_body_h']            global size (scalars)
        cfg['sizes'][key]   = [w, body_h]             per-character override
        cfg['layouts'][key] = [x, y, w, body_h]       saved rect (x/y untouched)

    Returns True IFF something actually changed, so the caller writes config
    only on a real correction and never per tick. IDEMPOTENT — a second call on
    a healed dict returns False and touches nothing (the fixpoint property the
    clamp_visible write-back already relies on). An ABSENT global key is left
    absent (the caller's own per-key defaulting owns it — healing a missing
    tile_w to the floor would silently demote it from the 384 default). A
    malformed entry is SKIPPED, not repaired: rewriting it here would hide a
    real fault somewhere else.

    Tk-free and Win32-free; mutates only the dict it is handed."""
    if not isinstance(cfg, dict):
        return False
    changed = False
    for key, floor in (("tile_w", MIN_TILE_W), ("tile_body_h", MIN_TILE_BODY_H)):
        if key not in cfg:
            continue
        healed = _floor_int(cfg[key], floor)
        if cfg[key] != healed:
            cfg[key] = healed
            changed = True
    sizes = cfg.get("sizes")
    if isinstance(sizes, dict):
        for key, val in list(sizes.items()):
            try:
                w, body_h = int(val[0]), int(val[1])
            except (TypeError, ValueError, IndexError, KeyError):
                continue
            healed = clamp_size(w, body_h)
            if healed != (w, body_h):
                sizes[key] = [healed[0], healed[1]]
                changed = True
    layouts = cfg.get("layouts")
    if isinstance(layouts, dict):
        for key, val in list(layouts.items()):
            try:
                x, y = int(val[0]), int(val[1])
                w, body_h = int(val[2]), int(val[3])
            except (TypeError, ValueError, IndexError, KeyError):
                continue
            healed = clamp_size(w, body_h)
            if healed != (w, body_h):
                layouts[key] = [x, y, healed[0], healed[1]]
                changed = True
    return changed


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


def snap_rect(rect, others, threshold=SNAP_THRESHOLD_PX, screens=()):
    """Magnetically snap a moving tile's top-left to nearby OTHER tiles' edges
    and to the DESKTOP borders.

    `rect` and each of `others` are (x, y, w, h). Returns the snapped (x, y).
    The two axes are handled INDEPENDENTLY; on each axis the candidate whose
    distance to the current coordinate is smallest AND within `threshold` wins
    (ties keep the first candidate seen). If no candidate is within `threshold`
    on an axis, that coordinate is returned unchanged.

    Per neighbour, the candidate positions are:
      X: butt  left↔right  -> x = ox + ow      (stick to the right of it)
         butt  right↔left  -> x = ox - w       (stick to the left of it)
         align left↔left   -> x = ox
         align right↔right  -> x = ox + ow - w
      Y: butt  top↔bottom  -> y = oy + oh      (stick below it)
         butt  bottom↔top  -> y = oy - h       (stick above it)
         align top↔top     -> y = oy
         align bottom↔bot. -> y = oy + oh - h

    `screens` are DESKTOP rects — one per monitor, or a single virtual-desktop
    rect — in this module's usual (x, y, w, h) convention, NOT the
    (x0, y0, x1, y1) EDGES the win32 _virtual_screen_bounds() hook returns
    (callers convert, exactly as clamp_visible already demands). Each screen
    contributes flush-to-border candidates:
      X: flush left -> x = sx        flush right  -> x = sx + sw - w
      Y: flush top  -> y = sy        flush bottom -> y = sy + sh - h
    They are weighed FIRST, so an exact tie between a desktop border and a
    neighbour edge resolves to the BORDER. Without them, a neighbour parked a
    few px inside a border owns that border's whole catch band and the border
    itself becomes UNREACHABLE while snapping is on — with the grid-arrange
    origin at (10, 10) and a 12 px threshold that is exactly what happened to
    the top and left of the desktop. A screen's top is NOT assumed to be 0: a
    display stacked above the primary has a negative one.

    Pure (no Tk / Win32). `others` must ALREADY exclude the moving rect — this
    function does not self-exclude (a caller that leaves the moving rect in
    `others` gets a harmless self-alignment no-op, never a crash)."""
    x, y, w, h = rect
    best_x, best_dx = x, threshold + 1
    best_y, best_dy = y, threshold + 1
    for s in screens:
        sx, sy, sw, sh = s
        for cand in (sx, sx + sw - w):                  # x: flush left / right
            d = abs(x - cand)
            if d <= threshold and d < best_dx:
                best_dx, best_x = d, cand
        for cand in (sy, sy + sh - h):                  # y: flush top / bottom
            d = abs(y - cand)
            if d <= threshold and d < best_dy:
                best_dy, best_y = d, cand
    for o in others:
        ox, oy, ow, oh = o
        for cand in (ox + ow, ox - w, ox, ox + ow - w):     # x: butt, butt, align, align
            d = abs(x - cand)
            if d <= threshold and d < best_dx:
                best_dx, best_x = d, cand
        for cand in (oy + oh, oy - h, oy, oy + oh - h):     # y: butt, butt, align, align
            d = abs(y - cand)
            if d <= threshold and d < best_dy:
                best_dy, best_y = d, cand
    return best_x, best_y


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
