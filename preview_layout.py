"""Pure geometry/cycling math for native previews. No Tk, no ctypes — fully unit-tested.
All rects are (x, y, w, h) in physical px, virtual-screen coordinate space."""
from __future__ import annotations

EDGE_SNAP_MIN = 20          # EVE-O parity: max(20, w // 10)
LOGIN_STACK_STEP = 24
# A cycle-group member naming a whole ACCOUNT by its numeric id (design §9.4),
# e.g. "acct:2499436". EVE character names cannot contain ":", so this token can
# never collide with a char key. THE single owner of the cycle-member account
# token format — fc_gui builds/parses it through resolve_cycle_members and this
# constant, never a bare literal. (Distinct in ROLE from fc_gui's account-SLOT
# layout prefix, which happens to share the "acct:" text but keys layouts by
# account LABEL, not id, in a different store.)
ACCT_MEMBER_PREFIX = "acct:"
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


def full_h(body_h, strip_h):
    """A tile's FULL on-screen height: its body plus its caption strip.

    THE conversion seam FOR THE SETTINGS PATH, with `body_h_from_full` right
    below it as the exact inverse. Everything inside FCPreview measures a tile in
    `body_h` — config, clamp_size, fit_body_h, every place() call — but the
    number a USER sees is the whole window, so the Settings "Tile size [w] × [h]"
    box displays this instead and converts at its own edges. Two functions, one
    file, tested as a pair, because this is the STRIP_H-conversion trap family:
    three separate bugs have come from a caller re-deriving `body_h + 20` by hand
    and getting the direction, the constant, or the font-scaled strip height
    wrong (see preview_tile.strip_h_for — the strip is NOT always STRIP_H).

    SCOPE, precisely: this pair owns the spinbox-display ↔ stored-`tile_body_h`
    crossing and nothing else. It is NOT the project's only STRIP_H arithmetic
    and was never meant to be — the tile GEOMETRY paths (preview_tile's own
    strip/thumb layout) and the FC HUD rect seam in fc_gui each do their own,
    legitimately, against live widget state this pure module cannot see. The
    claim to defend is narrow: no OTHER code re-derives the settings box's unit.

    `strip_h` is passed IN rather than imported: preview_tile imports this
    module, so this module can never import preview_tile back, and the caller
    already knows which strip height applies (STRIP_H for the caption strip, 0
    for a tile that has none).

    The body is floored at MIN_TILE_BODY_H first, so a hand-edited config can
    never display — or round-trip back into — a height no tile could be seen at;
    junk (None, "", NaN, a word) reads as the floor and a junk `strip_h` reads
    as 0, matching _floor_int's config-load policy.

    DO NOT "harmonise" this with `fit_body_h` below, in either direction: it
    answers an unanswerable question with None and never floors, because ITS
    caller is deciding whether to resize a live tile and "no answer" must stay
    distinguishable from "resize to the floor". This pair is the opposite
    contract on purpose — its caller is a config/display path that must always
    produce SOME number. Making either one behave like the other is a
    regression, and fit_body_h's docstring carries the mirror of this warning.

    Returns an int."""
    return _floor_int(body_h, MIN_TILE_BODY_H) + _floor_int(strip_h, 0)


def body_h_from_full(full_h, strip_h, min_body_h=MIN_TILE_BODY_H):
    """The BODY height inside a full on-screen tile height — `full_h`'s inverse.

    Exact round trip for every legal body height:
    `body_h_from_full(full_h(b, s), s) == b`. The floor is applied to the
    ANSWER (via `_floor_int` on `min_body_h + strip`), so a full height the user
    typed below the minimum window — or junk, or a negative — resolves to
    MIN_TILE_BODY_H rather than to a negative body: the settings Spinbox's
    `from_` bounds its ARROWS only and typed text reaches the var verbatim, so
    this is the real floor for anything the user can type.

    Shadows the module-level `full_h` inside this body deliberately — the
    parameter IS a full height, and nothing here needs the forward conversion.

    Like its forward twin, this floors junk rather than refusing it, and for the
    same reason: it serves the settings box, which must always end up with SOME
    storable number. `fit_body_h` below takes the OPPOSITE contract — None, never
    a floored fallback — because it drives a live-tile resize. Neither should be
    made to match the other; that "consistency" fix is a regression in both
    directions (fit_body_h's own docstring says so from its side).

    Returns an int."""
    strip = _floor_int(strip_h, 0)
    return _floor_int(full_h, min_body_h + strip) - strip


def aspect_fit(dest_w: int, dest_h: int, src_w: int, src_h: int):
    """Largest (x, y, w, h) inside dest preserving src aspect, centered.

    THE letterboxer for the whole FCPreview subsystem: preview_tile's
    _push_thumb_rect places every DWM thumbnail with it, and fit_body_h below is
    its inverse ("which body height leaves no letterbox?"). It lived in
    dwm_thumbs until 2026-08-22 — pure geometry with no ctypes in it — and
    keeping the two halves of one calculation in two modules is exactly how they
    drifted: fit_body_h used to round() the height this function TRUNCATES, so
    47% of the sizes it handed back were one px taller than the video that
    landed in them. Same owner, and fit_body_h verifies against this."""
    if src_w <= 0 or src_h <= 0 or dest_w <= 0 or dest_h <= 0:
        return (0, 0, max(dest_w, 0), max(dest_h, 0))
    scale = min(dest_w / src_w, dest_h / src_h)
    w = max(1, int(src_w * scale))
    h = max(1, int(src_h * scale))
    return ((dest_w - w) // 2, (dest_h - h) // 2, w, h)


# fit_body_h: how many single-px descents the verify step may make before it
# gives up and returns its first candidate. BOUNDED on purpose — a float
# round-trip on a pathological aspect ratio must never be able to spin the
# resize path. 4 is measured, not guessed, and it is EXACTLY SATURATED with no
# margin: across 22 realistic client window sizes x tile_w 120..960 (18,502
# cases) the deepest descent that ever succeeded was 4, hit by ordinary panels
# — 1280x800 and 2560x1600 at tile_w 186/187, 1728x1117 and 3456x2234 at
# 471/604/737 (10 cases in all; depth histogram 0:17829, 1:498, 2:110, 3:55,
# 4:10). Nothing in that sweep needed a 5th, so raising the budget buys nothing
# there; beyond 4 the 1 px bottom-band fallback below owns the answer, and the
# fallback is a deliberate trade, not a gap (see fit_body_h's own docstring).
# An earlier comment here claimed a measured deepest descent of 3 — that was
# measured over a resolution set that happened to exclude 1280x800-class
# panels. Re-measure before editing this number; do not carry it forward.
_FIT_VERIFY_STEPS = 4


def fit_body_h(tile_w, src_w, src_h, bottom_h=0, min_body_h=MIN_TILE_BODY_H):
    """The tile BODY height at which a `tile_w`-wide, aspect-preserved thumbnail
    fills the video area with NO black letterbox band above or below it.

    preview_tile._push_thumb_rect CENTRES the DWM thumbnail inside
    `body_h - bottom_h` (aspect_fit, directly above), so whatever vertical
    remainder the body has splits into two equal pure-black bands — the body
    frame is "#000000" — one under the caption strip, one above the bottom label
    strips. That is the "black bars top and bottom" users report. Sizing a tile
    to this height removes the remainder at the source; the letterbox in
    _push_thumb_rect stays as the safety net for every other size.

    HOW, and what is actually guaranteed. The candidate is aspect_fit's OWN
    float expression, truncated the way aspect_fit truncates it; it is then
    VERIFIED by calling aspect_fit and checking that the video really does start
    at y = 0 and really is as tall as the area it sits in. If it does not, the
    candidate descends up to `_FIT_VERIFY_STEPS` single px looking for one that
    does. Only heights at or BELOW the candidate can ever satisfy it — one px
    taller and aspect_fit is width-constrained again, so the video stays exactly
    where it was and the extra row is black — which is why the search only ever
    goes down. Measured over 22 realistic client window sizes x tile_w 120..960
    x bottom_h {0, 20, 40} (55,212 non-floored answers): ZERO residual bands.
    The deepest descent that succeeded there was 4 — the whole of
    `_FIT_VERIFY_STEPS`, saturated with no margin (see its comment above; an
    earlier version of this paragraph said 3, off a resolution set that excluded
    1280x800-class panels). Never go back to round(): rounding UP a height that
    aspect_fit then truncates left a 1 px band in 26,445 of those same answers —
    47.9% — and plain truncation without the verify step, 2,002 (3.6%).

    THE HONEST SCOPE, because `src_w`/`src_h` are the EVE client WINDOW size and
    a user can drag that window to any dimensions at all. On realistic client
    sizes the zero above holds. On arbitrary ones it does not quite: sweeping
    300,000 random (client size, tile_w, bottom_h) triples, 435 of them — 0.15%
    — keep a residual band. Every one of those 435 was exactly ONE px and
    exactly at the BOTTOM (aspect_fit's `(dest_h - h) // 2` floors a 1 px
    remainder to 0, so the video stays flush with the TOP of the video area),
    and nothing wider than 1 px appeared anywhere in the sweep. That is the
    fallback branch below: when no candidate inside the step budget verifies,
    the truncated candidate is returned as the best answer and the 1 px is
    accepted. The failures are not isolated noise — they come in systematic runs
    of consecutive tile_w for one client size (longest measured: 124) where the
    float round-trip sits on a plateau. A deeper budget WOULD clear them (the
    deepest descent any of the 435 needed was 157 px), which is precisely why
    the budget stays at 4: closing a 1 px band by handing back a tile up to
    157 px SHORTER than the aspect height is the worse answer, and an unbounded
    descent would let a pathological ratio spin the resize path. Only making
    aspect_fit's height exact would fix these properly. Keep the perspective the
    fix was for: the user-visible complaint is bands TENS of px tall.

    `body_h` EXCLUDES the caption strip (preview_tile.STRIP_H), like every other
    body_h in this subsystem, and INCLUDES `bottom_h`: the bottom strips are
    carved OUT of the body, so the video only ever gets what they leave.

    Returns None — NOT a floored fallback — whenever the inputs cannot answer
    the question: a non-positive or non-numeric tile_w / src_w / src_h (None,
    NaN, inf, "", a word), or a magnitude the aspect arithmetic cannot survive.

    MAGNITUDE is three-way, not one-way, and the ceiling is not on any single
    argument. Everything happens in `sh * (w / sw)` — the ANSWER itself — so the
    ceiling is on the answer: a result past the float range (~1.8e308) is
    unanswerable no matter which argument drove it there.
      * `tile_w` up  pushes the product up  -> None past the ceiling.
        fit_body_h(10**309, 1920, 1080) is None.
      * `src_h`  up  pushes the product up  -> None past the SAME ceiling; it
        does NOT "have room to go higher because it divides INTO it" (an earlier
        version of this passage claimed that and it was simply wrong — the
        overflow is in the multiply, not the division). fit_body_h(384, 1920,
        10**309) is None, the identical cutoff tile_w has at this aspect. The
        cutoff is joint, so it MOVES: at src_h = 100000 even tile_w = 10**307 is
        already None, and at src_h = 1 tile_w = 10**308 still answers.
      * `src_w`  up  pushes the product DOWN, so it never reaches the ceiling at
        all and never returns None. It underflows instead: `w / sw` goes to 0.0,
        `video_h` becomes 0, the verify loop breaks on `cand <= 0`, and
        _floor_int hands back the FLOOR. fit_body_h(384, 10**309, 1080) is 68,
        and so is 10**400 — no exception, no None, just MIN_TILE_BODY_H, which
        is the truthful answer for a source that wide anyway.
    There used to be a narrow SEAM inside that band, now closed. For `tile_w`
    between the float ceiling and the joint ceiling above (e.g. 1x-1.78x it at
    16:9), `video_h` itself survives — Python's int/int true division for
    `w / sw` never has to materialize float(tile_w), so it stays finite even
    when tile_w itself could not — but the verify loop's own aspect_fit call
    re-derives roughly `tile_w` a second time through a genuine float
    round-trip (`scale = dest_w / src_w`, then `src_w * scale`), and THAT
    multiplication can still silently overflow to inf even though tile_w
    never went through float() at all. For two releases that overflow escaped
    uncaught as a bare OverflowError — e.g. fit_body_h(19 * 10**307, 1920,
    1080) — breaking the "returns None, never raises" contract; no power of
    ten lands in the seam at 16:9, which is why it went unnoticed. It is now
    caught at the verify loop itself (narrowly, around just that call, so an
    unrelated TypeError/ValueError elsewhere in the loop still surfaces) and
    returns None like every other unanswerable magnitude. The blanket "past
    the float range returns None" now holds everywhere, with no seam left.

    Below the ceiling the answer is honest rather than absurd-rejecting:
    fit_body_h(10**300, 1920, 1080) really is a 300-digit height, because
    fitting a tile to a screen is the caller's job and there is no
    non-arbitrary magnitude cap to impose here. The None convention
    is deliberately UNLIKE _floor_int's junk-to-floor policy, which exists for
    the config-load path where SOME value has to be produced. Here the caller is
    deciding whether to resize a live tile, and "I have no answer" must stay
    distinguishable from "resize it to the floor" — quietly returning the floor
    would shrink a tile whose source size simply has not been queried yet. Do
    not harmonise the two.

    A negative or garbage `bottom_h` is treated as 0, and an absurd one is added
    verbatim — no magnitude cap on this one either. Unlike tile_w it is DERIVED
    state (which strips happen to be packed at this instant), never user config,
    and preview_tile._bottom_h() caps itself at 60% of the body, so there is
    nothing to defend against and no answer to withhold.

    The result is floored through _floor_int at `min_body_h`, so MIN_TILE_BODY_H
    stays the one owner of the minimum. A floored return does NOT satisfy the
    zero-letterbox property — a tile that narrow cannot: fit_body_h(120, 3440,
    1440) is 68, while the height that would actually fit the video is 50.

    Pure: no Tk, no ctypes, arguments never mutated. Deterministic, so the
    resize loop (compute -> resize -> compute again) settles instead of
    oscillating: the second answer always equals the first."""
    try:
        w, sw, sh = int(tile_w), int(src_w), int(src_h)
        if w <= 0 or sw <= 0 or sh <= 0:
            return None
        # aspect_fit's own expression, truncated the same way it truncates.
        video_h = int(sh * (w / sw))
    except (TypeError, ValueError, OverflowError):
        return None
    for step in range(_FIT_VERIFY_STEPS + 1):
        cand = video_h - step
        if cand <= 0:
            break                             # nothing left to test; floor owns it
        try:
            _fx, fy, _fw, fh = aspect_fit(w, cand, sw, sh)
        except (TypeError, ValueError, OverflowError):
            # video_h itself (line 234) survived — Python's int/int true-division
            # avoids materializing float(w) when the QUOTIENT fits float range —
            # but aspect_fit's own scale = dest_w / src_w then src_w * scale
            # round-trip can still overflow past the float ceiling for a huge
            # tile_w. That is unanswerable the same as any other magnitude past
            # the ceiling, so this candidate (and, at this magnitude, every
            # smaller one _FIT_VERIFY_STEPS could still try) does not verify;
            # say so honestly instead of returning the unverified truncated
            # candidate.
            return None
        if fy == 0 and fh == cand:            # the real letterboxer agrees
            video_h = cand
            break
    try:
        bottom = max(0, int(bottom_h))
    except (TypeError, ValueError, OverflowError):
        bottom = 0
    return _floor_int(video_h + bottom, min_body_h)


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


def resolve_cycle_members(members, live_identities, char_for_account):
    """Resolve cycle-group member tokens to live-client identities (design §9.4).

    `members` are already-normalized tokens (``str(m).strip().lower()``), each
    either a character key or an ``acct:<id>`` account token. `live_identities`
    is the set of identities currently on screen — a logged-in char's identity
    IS its key, an accounted login screen is ``login:<id>``, an unknown-account
    login is excluded (identity ``""``). `char_for_account` is a callable
    ``(account_id:int) -> char_key | None``: inject ``AccountMap.char_for_account``
    in production, or ``lambda _id: None`` when account identity is off / the map
    is absent (char-only resolution runs the SAME code path). Pure — this module
    imports neither the account service nor Tk.

    Per member, in order (order preserved, duplicates left untouched):
      * a character-key member -> itself iff it is a live identity, else dropped;
      * an ``acct:<id>`` member -> that account's live character key when
        ``char_for_account(<id>)`` names one that is live; else the account's
        ``login:<id>`` tile when THAT is live; else dropped. **The character WINS
        over the login** when both resolve for one account.
      * an ``acct:`` token whose id is missing or non-integer -> dropped.

    Returns the resolved identity list. ``cycle_next(strict=True)`` re-filters it
    against the live set, so a non-live char dropped here versus dropped by the
    ring are equivalent; dropping here keeps the intent explicit AND makes the
    login fallback correct — only a LIVE login survives, and only when no live
    char on the account outranks it.
    """
    resolved = []
    for m in members:
        if m.startswith(ACCT_MEMBER_PREFIX):
            try:
                account_id = int(m[len(ACCT_MEMBER_PREFIX):])
            except (TypeError, ValueError):
                continue                                  # acct:<not-an-int> -> drop
            char = char_for_account(account_id)
            if isinstance(char, str):
                char = char.strip().lower()
            if char and char in live_identities:
                resolved.append(char)                     # char WINS over login
                continue
            login = "login:%d" % account_id
            if login in live_identities:
                resolved.append(login)
            # neither the account's char nor its login is live -> drop it
        elif m in live_identities:
            resolved.append(m)
    return resolved
