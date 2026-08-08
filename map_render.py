"""pygame glow renderer for the star map — the ONLY module that imports pygame.

Spike-B-locked constraints: plain SRCALPHA surfaces, NO pygame.display init
anywhere (coexists with pygame.mixer TTS), unconverted blits. Rendering is
headless; Phase C blits the finished frame into Tk via surface_to_ppm().
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass

import pygame
import pygame.gfxdraw as gfx

import map_overlays as mo   # pure sov_color hash for the sovereignty tint (Task 33)

# --- palette (POC v2 / Spike A approved) -----------------------------------
BG = (10, 10, 20)
SEC_HI = (0x33, 0xB5, 0xE5)
SEC_LOW = (0xFF, 0xB3, 0x47)
SEC_NULL = (0xCC, 0x22, 0x33)
LABEL_COLOR = (219, 226, 237)   # system-name text; brightened past #d8d8e0 (owner
                                # 2026-07-12 readability pass -- was (200,210,225),
                                # a touch dim over bright range/threat washes)
LABEL_OUTLINE = (10, 10, 20)    # near-black (#0a0a14, the space-bg family) 1px ring
                                # baked under system-name labels so they read on ANY
                                # background (green range, purple threat, sov, nebula)
REGION_LABEL_COLOR = (150, 165, 195)
HUB_IDS = frozenset({30000142, 30002187, 30002659, 30002053, 30002510})

# Electric blue for friendly Ansiblex bridges (owner request 2026-07-10): a
# glowing blue line between the two bridged systems, drawn UNDER the node glows
# so systems stay readable. Three dim() passes (wide dim / mid / bright aaline).
BRIDGE_BLUE = (0x3A, 0x86, 0xFF)

# Red-orange kill-heat under-glow (Task 30): an additive glow at each hot system,
# radius scaled by the 0..1 heat intensity, drawn UNDER the node glows (like the
# bridge lines) so the system cores stay readable on top of the heat.
HEAT_COLOR = (0xFF, 0x5A, 0x2E)

# Sovereignty tint (Task 33): a dim per-alliance blob washed BEHIND everything
# else. FIXED radius (not zoom-scaled) so the disc-sprite cache stays bounded by
# the alliance COUNT (color) alone, not color x radius -- and it is comfortably
# larger than the node glows (node_metrics caps glow at 18 px) so adjacent
# same-alliance systems merge into one regional wash instead of reading as
# separate dots. Dimness alone does NOT keep the wash off white: _draw_sov
# composes the blobs per-pixel with BLEND_RGB_MAX (max(a, a) = a) onto a scratch
# surface, so any number of overlapping same-alliance discs tops out at a single
# disc's peak channel (~102) -- the wash goes FLAT, never additive toward white.
# (An earlier build ADD-stacked the blobs and DID wash dense single-alliance
# regions to ~(245,245,235); the MAX compose is the fix.)
SOV_RADIUS = 34

# Range-overlay in-range GREEN accent (owner 2026-07-12): the old range overlay
# left in-range systems at FULL native colour and dimmed the rest -- so an in-range
# NULLSEC system stayed bright red while out-of-range nulls dimmed to darker red, a
# red-on-red distinction the owner found hard to read. In-range systems now also get
# a green glow aura (a hair LARGER than the node glow so it peeks out AROUND the
# node), MAX-composed on the shared scratch then ADD-blitted ONCE (the _draw_sov
# house pattern) so dense in-range clusters wash FLAT green and never toward white.
# Green reads clearly against red nullsec (#cc2233), amber lowsec, cyan highsec, and
# stays distinct from the dim violet threat wash below. The green is an ACCENT: the
# node's own colour + white core still ride on top, so in-range stays "bright as
# today" -- the green is the readability cue, not a recolour.
#
# Readability pass (owner 2026-07-12): the first cut was a floodlight -- too LARGE
# (pad 8) and too LIGHT green, which drowned the system-name labels and hurt to read.
# Tighten the halo (pad 3, hugs the node) and DIM the glow to ~57% brightness. The
# RGB is scaled BEFORE the sprite build (RANGE_GREEN_GLOW), so the whole additive
# gradient -- and its MAX-compose ceiling -- drops proportionally while the hue is
# preserved: in-range still reads clearly as green, just no longer a wash.
RANGE_GREEN = (0x39, 0xFF, 0x8C)          # reference in-range hue (bright green)
RANGE_GREEN_DIM = 0.57                     # glow brightness vs RANGE_GREEN (~57%)
RANGE_GREEN_GLOW = (int(RANGE_GREEN[0] * RANGE_GREEN_DIM),
                    int(RANGE_GREEN[1] * RANGE_GREEN_DIM),
                    int(RANGE_GREEN[2] * RANGE_GREEN_DIM))   # (32, 145, 79)
RANGE_GLOW_PAD = 3            # green aura radius = node glow_r + this (tight halo)

# The WASH radius (threat purple / friendly blue / the split overlap blob) --
# node glow_r + this. Named because the SPLIT blob (below) must land on EXACTLY
# the same disc as the full washes it replaces: a half sprite at a different
# radius would read as a seam, not as one disc cut down the middle.
WASH_GLOW_PAD = 8

# Threat projection wash (owner 2026-07-12): the projected-reach warning under a
# threatened system, switched from RED to a DIM violet so it is distinguishable from
# red nullsec nodes, blue Ansiblex bridges, green range, and amber intel (the owner
# found the old red "hard to distinguish"). Drawn UNDER the node glows as a MAX-
# composed background wash (same white-out-proof compose as sov/range). NOTE: the
# hostile-staging DIAMONDS stay RED (map_tab._draw_diamond, ov_staging) -- those are
# point markers of a DIFFERENT semantic (where hostiles ARE, not their reach), so
# their red is intentional and untouched.
THREAT_PURPLE = (0x8e, 0x5b, 0xd6)

# Friendly-staging PROJECTION wash (owner ask): the projected jump/bridge reach of
# your OWN stagings -- a SECOND, opt-in halo drawn beside the hostile purple threat.
# A deep azure BLUE (#3d7dd6), chosen to read DISTINCT from every neighbour: the
# purple hostile wash (blue's GREEN channel sits ABOVE red, whereas purple's RED sits
# above green -- opposite low-channel ordering), the green range aura, and the
# BRIGHTER blue Ansiblex bridge LINES (#3a86ff) -- this is an area WASH one hue deeper
# than the bridges so the two blues separate by both form and shade. MAX-composed onto
# the shared scratch then ONE additive blit, exactly like THREAT_PURPLE, so overlapping
# friendly blobs top out FLAT (never white). A system inside BOTH the hostile and
# friendly reach no longer stacks the two washes at all -- since 2026-08-08 it draws
# ONE split disc instead (see SPLIT_CLASSES below), which is what removed the
# blue-violet ambiguity this comment used to describe. The white-out guarantee now
# spans FOUR additive passes (range accent, threat, friendly, split): each MAX-composes
# on its own scratch and contributes ONE additive blit, and the passes are mutually
# exclusive per system (a split system is skipped by every full pass whose colour its
# blob already carries), so no pixel can take more than two of them -- measured on a
# dense worst-case cluster, the summed contribution never trips all three channels
# past 200.
FRIENDLY_BLUE = (0x3d, 0x7d, 0xd6)

# --- overlap SEMICIRCLES (owner 2026-08-08) ---------------------------------
# Two range effects on ONE system used to ADD into a single ambiguous blob (purple
# + blue -> blue-violet; purple + green accent -> a muddy rim), so the owner could
# not tell WHICH reaches covered a system. A system carrying TWO of the three
# effects now draws ONE disc at the wash radius split down its vertical diameter:
# the LEFT half is always the hostile THREAT_PURPLE, the RIGHT half is the other
# effect (friendly blue, or the DIMMED range green -- never the raw neon). The two
# halves come from the SAME sprite at the SAME radius, so they read as one seamless
# disc, not two lobes.
#
# THREE effects cannot be split three ways and still be legible (owner: "showing 2
# at a time is viable, but 3 are not"), so a triple-overlap system draws the
# purple|blue split -- NO green anywhere on the blob -- and moves the jump-range
# signal onto its NAME, which gains a yellow glow (LABEL_GLOW_YELLOW below).
#
# Scope note: friendly + range WITHOUT threat (class "FR") is deliberately
# UNCHANGED -- blue wash plus the tight green accent already read fine, and the
# owner's ask names threat overlaps only. Splitting it would cost the green accent
# for no readability win.
SPLIT_CLASSES = frozenset({"TF", "TR", "TFR"})

# System-name label tints (owner 2026-08-08 readability pass). A system carrying
# EXACTLY ONE effect tints its NAME in a light variant of that effect's hue, so the
# name itself says which reach covers it; two effects keep LABEL_COLOR (the split
# blob already carries that information) and the triple keeps LABEL_COLOR plus the
# yellow glow. Every variant keeps the dark LABEL_OUTLINE ring, which is what makes
# them legible over their own wash.
#
# Derivation (measured, not eyeballed -- contrast ratios are WCAG relative
# luminance on this box):
#   threat   THREAT_PURPLE blended 55% toward white -> #ccb5ed, 10.7:1 vs BG,
#            2.5:1 vs its own wash peak
#   friendly FRIENDLY_BLUE blended 55% toward white -> #a8c4ed, 11.1:1 vs BG,
#            2.4:1 vs its own wash peak
#   range    RANGE_GREEN desaturated 50% toward its own luminance THEN 12% toward
#            white -> #88dfac, 12.4:1 vs BG, 2.7:1 vs the dimmed accent. The
#            desaturation step is the point: the owner explicitly rejected bright
#            green, and RANGE_GREEN raw (#39ff8c, G=255) is exactly that neon.
LABEL_TINT_THREAT = (0xcc, 0xb5, 0xed)
LABEL_TINT_FRIENDLY = (0xa8, 0xc4, 0xed)
LABEL_TINT_RANGE = (0x88, 0xdf, 0xac)

# Triple-overlap (threat + friendly + range) NAME glow: a warm gold halo painted
# UNDER the outlined glyphs. Yellow is the one hue not already spoken for by a
# wash (purple/blue/green) or a marker (red staging, amber intel, cyan/orange/red
# sec tints), so it cannot be mistaken for one of them.
LABEL_GLOW_YELLOW = (0xff, 0xd8, 0x4d)
LABEL_GLOW_PAD = 3                       # halo reach in px on every side
LABEL_GLOW_RING_ALPHA = (200, 130, 70)   # per-ring alpha, inner -> outer falloff

# System-name label offset FLOOR, in px from the node centre (owner 2026-08-08:
# "the text should also be slightly farther from the sphere so the first letter is
# readable"). Was a flat 7, which parked the first glyph inside the green range
# accent. Measured at a shallow band-C framing (glow_r 4-6, core_r 1-2): the solid
# white core ends by +2 px and the range accent's added ink is gone well before
# +11, so a label surface at +11 puts its first glyph pixel (the outlined composite
# carries a 1 px ring) at +12 -- clear of both.
#
# A FLAT offset is not enough on its own: node_metrics grows glow_r 4 -> 18 with
# zoom, and the accent sprite grows with it, so at px_per_edge 64+ the accent inks
# out to ~+20 and a fixed +11 label would sit back inside it -- exactly the overlap
# the owner complained about, returning at the zoom FCs actually work at. The live
# offset is therefore computed per frame by label_offset_x() below; this constant is
# its FLOOR. Band-M hub labels keep their own flat +7: that band draws no per-system
# labels to crowd them, and the owner's ask is scoped to the system-label band.
LABEL_OFFSET_X = 11

# Infrastructure count chips (Task 5): a small rounded badge per system carrying
# its structure count, tinted by the DOMINANT category. Drawn in the label pass
# (rides the SAME zoom LOD as system labels -- st.system_labels), so chips only
# appear at the band where labels already show. Stale systems (all entries older
# than the store threshold) dim their fill; the count text auto-picks dark/light
# by fill luminance so it stays legible on every tint and in both states. Colors
# are the plan's §3.8 category palette.
INFRA_CHIP_COLORS = {
    "citadel": (0x4d, 0x9d, 0xe0),
    "engineering": (0xb0, 0x85, 0xf5),
    "refinery": (0xe0, 0xa9, 0x4d),
    "gate": (0x37, 0xd1, 0xc0),
    "flex": (0x9a, 0xa7, 0xb5),
    "npc": (0x66, 0x77, 0x88),
    "unknown": (0x88, 0x99, 0xaa),
}
INFRA_CHIP_PX = 12                # count-text pixel size (reuses the label font)
INFRA_CHIP_STALE_DIM = 0.6        # dim the fill 40% when the system is stale
INFRA_CHIP_TEXT_DARK = (18, 22, 30)
INFRA_CHIP_TEXT_LIGHT = (238, 242, 248)

# Nebula additive-glow brightness (fraction of the region tint). Lowered
# 0.16 -> 0.11 at the Phase B checkpoint so dense-region blobs stop fusing
# into hot white cores at universe zoom.
NEBULA_DIM = 0.11

_FONT_NAME = "segoeui"


def sec_color(sec: float) -> tuple[int, int, int]:
    if sec >= 0.45:
        return SEC_HI
    return SEC_LOW if sec > 0.0 else SEC_NULL


# Index form of sec_color for the hot edge/system loops (Task 18 Step 3): the map
# has only THREE possible node/edge tints, so a system's colour and an edge's
# colour can be precomputed ONCE as an index into _SEC_TINTS. _SEC_TINTS[_sec_idx(sec)]
# is the exact object sec_color(sec) returns, so downstream dim()/blits are
# byte-identical -- this is a lookup-table cache, not an output change.
_SEC_TINTS = (SEC_HI, SEC_LOW, SEC_NULL)


def _sec_idx(sec: float) -> int:
    if sec >= 0.45:
        return 0
    return 1 if sec > 0.0 else 2


def dim(color: tuple[int, int, int], factor: float) -> tuple[int, int, int]:
    return (int(color[0] * factor), int(color[1] * factor), int(color[2] * factor))


def classify_effects(sid: int, bright, halo, friendly) -> str:
    """Which of the three range effects cover ``sid``, as a canonical class string.

    Returns one of ``""``, ``"T"``, ``"F"``, ``"R"``, ``"TF"``, ``"TR"``, ``"FR"``,
    ``"TFR"`` -- letters ALWAYS in threat / friendly / range order, so the class is
    a stable dict key and a stable test parameter. ``bright``/``halo``/``friendly``
    are the three ``TintSpec`` sets and may be None (= no such effect this frame).
    Pure: no rendering state, no side effects.

    T = hostile threat projection (``TintSpec.halo``, purple)
    F = friendly staging projection (``TintSpec.friendly``, blue)
    R = jump-range check (``TintSpec.bright``, green)"""
    cls = ""
    if halo and sid in halo:
        cls = "T"
    if friendly and sid in friendly:
        cls += "F"
    if bright and sid in bright:
        cls += "R"
    return cls


def split_right_color(cls: str) -> tuple[int, int, int] | None:
    """RIGHT-half hue of the split overlap blob (the LEFT half is always
    THREAT_PURPLE). Friendly BLUE outranks range green, which is what makes the
    triple case (``"TFR"``) purple|blue with NO green on the blob -- the jump-range
    signal moves to the label's yellow glow instead. Returns None for a class that
    does not split."""
    if cls not in SPLIT_CLASSES:
        return None
    if "F" in cls:
        return FRIENDLY_BLUE
    # Wash-brightness parity: the range accent is drawn DIMMED (RANGE_GREEN_GLOW),
    # so the green half must be too -- raw RANGE_GREEN would out-shout the purple
    # half and break the "one disc" read.
    return RANGE_GREEN_GLOW


def label_offset_x(glow_r: int) -> int:
    """Horizontal gap from a system's node centre to its name label, for this
    frame's node size. Pure -- the SINGLE source of the offset rule, shared by the
    label pass and its test.

    ``LABEL_OFFSET_X`` is the floor (shallow zoom, where it already clears
    everything). Past that the offset tracks the node: the green range accent is a
    sprite of radius ``glow_r + RANGE_GLOW_PAD``, so clearing its rim plus 2 px of
    breathing room is what keeps the first letter readable at EVERY zoom -- the
    owner's complaint was about zoomed-in framings, where node_metrics has grown
    glow_r toward its cap of 18 and a flat offset falls back inside the accent."""
    return max(LABEL_OFFSET_X, glow_r + RANGE_GLOW_PAD + 2)


def label_style(cls: str):
    """``(text_colour, glow_colour_or_None)`` for a system-name label of effect
    class ``cls``. Pure -- the single source of the label-tint rule:

      * exactly ONE effect -> a readable light variant of that effect's hue
      * the TRIPLE overlap -> LABEL_COLOR plus the yellow glow (the glow IS the
        jump-range signal the blob gave up)
      * anything else (no effect, or a two-effect split blob) -> LABEL_COLOR
        unchanged; the blob already carries the information."""
    if cls == "T":
        return LABEL_TINT_THREAT, None
    if cls == "F":
        return LABEL_TINT_FRIENDLY, None
    if cls == "R":
        return LABEL_TINT_RANGE, None
    if cls == "TFR":
        return LABEL_COLOR, LABEL_GLOW_YELLOW
    return LABEL_COLOR, None


def _declutter(items, cell_w: float, cell_h: float) -> list:
    """Occupancy-grid label suppression. `items` is an iterable of (key, sx, sy)
    in PRIORITY order (highest-priority first); keeps the first item to claim each
    (col, row) screen cell and drops later overlaps. Returns the kept (key, sx, sy)
    tuples in input order. Shared by the system-label and region-label branches."""
    occupied: set[tuple[int, int]] = set()
    kept = []
    for key, sx, sy in items:
        cell = (int(sx // cell_w), int(sy // cell_h))
        if cell in occupied:
            continue
        occupied.add(cell)
        kept.append((key, sx, sy))
    return kept


# --- zoom bands (spec §2.4; thresholds tunable) -----------------------------
@dataclass(frozen=True)
class BandStyle:
    """Per-band draw toggles. Node SIZES (glow radius, core radius) are no longer
    band constants -- they are computed per frame from zoom depth by
    node_metrics(), so deep zoom stays elegant at EVERY level (owner: cores read
    too large close-in), not just band C. BandStyle now carries only the edge /
    label / ring choices."""
    edge_width: int       # widest (dim) pass; 0 = aaline only
    edge_dim: float       # brightness of the aaline pass
    system_labels: bool
    hub_labels: bool      # spec §2.4: band M labels hub systems alongside regions
    label_px: int
    core_ring: bool = True  # draw the aacircle sec-color ring around the white core


BAND_STYLES = {
    # U keeps the ring OFF so ~2px-spaced systems stop fusing into white-cored
    # blobs; M/C request the sec-colour ring but node_metrics still gates it on
    # core_r >= 2 (a ring around a 1px core reads as a blob -- the close-in
    # complaint).
    "U": BandStyle(edge_width=0, edge_dim=0.28, system_labels=False,
                   hub_labels=False, label_px=0, core_ring=False),
    "M": BandStyle(edge_width=2, edge_dim=0.55, system_labels=False,
                   hub_labels=True, label_px=13),
    "C": BandStyle(edge_width=3, edge_dim=0.55, system_labels=True,
                   hub_labels=False, label_px=13),
}


def node_metrics(px_per_edge: float) -> tuple[int, int]:
    """(glow_radius, core_radius) from zoom depth. px_per_edge = cam.scale x
    median world edge length. Grows softly, capped so deep zoom stays elegant
    (owner: cores read too large close-in)."""
    glow = int(min(18.0, max(4.0, px_per_edge * 0.28)))
    core = 1 if px_per_edge < 22 else 2
    return glow, core


def pick_band(visible_count: int) -> str:
    if visible_count > 2500:
        return "U"
    return "M" if visible_count >= 300 else "C"


@dataclass(frozen=True)
class TintSpec:
    """Base-layer tinting (spec §5.1/§5.2): range overlay brightens `bright`
    and dims everything else; `halo` under-glows the hostile-staging reach in
    purple, and `friendly` under-glows the friendly-staging reach in blue (owner
    ask -- a second, opt-in projection sharing the same ship-class range). Any
    field None means that pass is skipped, so an all-None spec is byte-identical
    to no tint at all."""
    bright: frozenset[int] | None = None    # None = no range tint
    halo: frozenset[int] | None = None      # None = no hostile-staging projection
    friendly: frozenset[int] | None = None  # None = no friendly-staging projection

    def key(self) -> tuple:
        return (tuple(sorted(self.bright)) if self.bright is not None else None,
                tuple(sorted(self.halo)) if self.halo is not None else None,
                tuple(sorted(self.friendly)) if self.friendly is not None else None)


# --- cached asset factories --------------------------------------------------
# MP6 (2026-07-28): the two UNBOUNDED render caches. FrameCache was already
# bounded (it holds exactly one surface); these two grew for the life of the
# process -- every distinct system name ever labelled (5,485 x ~3.87 KB measured
# = 21 MB at one band) and every distinct alliance colour ever discked. Worse,
# that growth is an INPUT to the M4 conservation throttle (map_tab._m4_note_apply
# arms on dur > STALL_MS AND working-set growth > STALL_WS_KB), so the label
# cache was feeding the very throttle that measures it. Both are now LRU-capped.
#
# Sizing (all measured on this box 2026-07-28, not estimated):
#  * LABEL_CACHE_MAX -- per-system name labels are drawn ONLY in band "C", and
#    pick_band() returns "C" only below 300 visible systems, so ONE frame can
#    request at most 299 distinct name surfaces (the 96x24 declutter grid thins
#    it further) plus the infra count chips that share the same LOD. The M/U
#    bands ask for region anchors (~113) + 5 hub names. Worst single frame is
#    therefore ~500 distinct keys; 1024 is 2x that, so a steady viewport never
#    evicts anything it is about to re-ask for, and the cache is hard-bounded at
#    ~3.96 MB instead of ~21 MB. A rebuild costs 42 us, so even a thrashing
#    frame would be survivable -- but the headroom means it cannot happen.
#  * DISC_CACHE_MAX -- _draw_sov walks the WHOLE canonical sov tuple every frame
#    (~4,200 pairs) and culls by screen position, so a fit-universe framing can
#    legitimately ask for every distinct alliance colour in ONE frame. That set
#    is provably small: map_overlays.sov_color is a fixed-S/V hue curve whose
#    codomain is exactly 339 distinct RGB triples (measured by enumerating
#    500,000 alliance ids -- 339 for every range tried), and SOV_RADIUS is fixed,
#    so this cache was ALREADY structurally bounded before this constant existed
#    -- 339 entries, ~6.3 MB, no matter how EVE's alliance count grows. 512 sits
#    1.5x above that ceiling, so DISC_CACHE_MAX recovers no memory today; it is a
#    guard against a FUTURE caller that varies the radius, not a memory fix --
#    unlike LABEL_CACHE_MAX above, which IS this batch's actual memory win
#    (~21 MB -> <=3.96 MB). Eviction here can only ever be reached by such a
#    radius-varying caller, and thrash (387 us per disc build, 339 of them =
#    131 ms/frame) is structurally impossible today.
LABEL_CACHE_MAX = 1024
DISC_CACHE_MAX = 512


class SpriteFactory:
    """Procedural radial glow sprites, cached by (color, radius).

    Each sprite is a SMOOTH radial gradient: _N concentric filled circles drawn
    from a wide dim ring (radius = ss/2) inward to a bright core (radius = ss/8)
    on a supersampled SRCALPHA surface, then smoothscaled DOWN to (2r, 2r). The
    supersample + downscale melts the discrete rings into a soft falloff, instead
    of the blocky plateau+rim a single small upscaled disc produced (owner: live
    app read blockier than the POC).

    Mechanism (verified empirically, see report): the sprites are blitted with
    BLEND_RGB_ADD, which in SDL/pygame IGNORES source per-pixel alpha and adds
    only RGB -- (r,g,b,10) and (r,g,b,255) add identically. So the VISIBLE
    gradient must live in the RGB channels: each ring's colour is scaled by a
    quadratic-eased weight (_RGB_OUT.._RGB_IN). The alpha channel is ALSO ramped
    (_A_OUT.._A_IN) as a faithful gradient descriptor (sampled by tests / usable
    by any future alpha-respecting blit), but it does not affect the additive
    render. Cache key is unchanged, so this is a drop-in for every caller."""

    _N = 8                          # concentric rings
    _SS_MAX = 512                   # supersample-surface cap (bounds big-nebula cost)
    _A_IN, _A_OUT = 70, 6           # inner/outer alpha -- gradient descriptor only
    _RGB_IN, _RGB_OUT = 0.90, 0.05  # inner/outer colour weight -- the ADDITIVE gradient

    def __init__(self, disc_max: int = DISC_CACHE_MAX) -> None:
        # glow(): keyed by (colour, radius) off a fixed set of callers (the three
        # sec tints, range-green, threat-purple, friendly-blue, per-region nebula
        # dims) x a handful of bucketed radii -- self-bounding by construction, but
        # NOT the "small" palette this comment used to claim: measured worst case
        # across all call sites is <=204 distinct keys, ~10.8 MB ceiling, dominated
        # by the radius-240 nebula sprites (921 KB each x3 tints x10 radius
        # buckets). Still bounded, so it stays a plain dict for now -- a future
        # slimming candidate. Only the sov disc cache needed an LRU bound (MP6).
        self._cache: dict[tuple[tuple[int, int, int], int], pygame.Surface] = {}
        # half_glow(): keyed by (colour, radius, side). Bounded by construction and
        # far tighter than glow() -- only the overlap-blob call sites reach it, i.e.
        # THREAT_PURPLE on the left plus FRIENDLY_BLUE / RANGE_GREEN_GLOW on the
        # right (3 colours) x the same handful of bucketed wash radii x 2 sides.
        self._half_cache: dict[tuple[tuple[int, int, int], int, str],
                               pygame.Surface] = {}
        self._disc_cache: "OrderedDict[tuple[tuple[int, int, int], int], pygame.Surface]" \
            = OrderedDict()
        self._disc_max = max(1, int(disc_max))

    def glow(self, color: tuple[int, int, int], radius: int) -> pygame.Surface:
        key = (color, radius)
        got = self._cache.get(key)
        if got is None:
            got = self._build(color, radius)
            self._cache[key] = got
        return got

    def _build(self, color: tuple[int, int, int], radius: int) -> pygame.Surface:
        target = max(2 * radius, 2)
        ss = max(min(8 * radius, self._SS_MAX), target)   # supersample edge, >= target
        src = pygame.Surface((ss, ss), pygame.SRCALPHA)
        c = ss // 2
        outer, inner = ss / 2.0, ss / 8.0
        n = self._N
        for i in range(n):                                # outer(dim) -> inner(bright)
            u = i / (n - 1)
            e = u * u                                     # quadratic ease -> long soft tail
            rr = max(int(round(outer - (outer - inner) * u)), 1)
            a = int(round(self._A_OUT + (self._A_IN - self._A_OUT) * e))
            w = self._RGB_OUT + (self._RGB_IN - self._RGB_OUT) * e
            col = (int(color[0] * w), int(color[1] * w), int(color[2] * w), a)
            pygame.draw.circle(src, col, (c, c), rr)
        return pygame.transform.smoothscale(src, (target, target))

    # --- half glow (overlap semicircles, owner 2026-08-08) -------------------
    def half_glow(self, color: tuple[int, int, int], radius: int,
                  side: str) -> pygame.Surface:
        """One HALF of the cached radial glow, at the FULL sprite footprint.

        ``side`` is ``"L"`` or ``"R"``; the cut is the sprite's vertical diameter at
        column ``radius``. The left half owns columns ``[0, radius)`` and the right
        half ``[radius, 2*radius)`` -- exactly complementary, so a left and a right
        half of the same radius MAX-compose back into the full disc with NO gap
        column and NO double-painted column. That is what lets two different hues
        read as one seamless disc cut down the middle instead of two lobes.

        Built by COPYING the full sprite and zeroing the other half (``Surface.fill``
        writes RGBA directly, it does not blend), so the kept half is byte-identical
        to the full glow -- same radial falloff, same MAX-compose ceiling. The
        zeroed half is transparent BLACK, which contributes nothing under either
        BLEND_RGB_MAX (max(x, 0) = x) or BLEND_RGB_ADD."""
        key = (color, radius, side)
        got = self._half_cache.get(key)
        if got is None:
            full = self.glow(color, radius)
            w, h = full.get_size()
            mid = w // 2
            got = full.copy()
            got.fill((0, 0, 0, 0),
                     pygame.Rect(mid, 0, w - mid, h) if side == "L"
                     else pygame.Rect(0, 0, mid, h))
            self._half_cache[key] = got
        return got

    # --- sov disc (Task 33 fix) ---------------------------------------------
    # A SOFT-EDGED disc for the sovereignty wash: a broad flat plateau at full
    # colour weight (1.0) out to _DISC_PLATEAU of the radius, then a smoothstep
    # falloff to 0 at the rim. UNLIKE .glow()'s peaky bright-core ramp, this reads
    # as an EVEN region fill -- which is what makes _draw_sov's per-pixel
    # BLEND_RGB_MAX compose produce a FLAT wash: overlapping same-alliance discs
    # take max(a, a) = a, so ANY density of blobs tops out at ONE disc's peak
    # channel (~102) instead of ADD-summing toward white. Kept in a SEPARATE cache
    # from .glow() (the same (color, radius) key would otherwise collide); radius
    # is fixed (SOV_RADIUS) so the cache stays bounded by the alliance count.
    _DISC_N = 16                    # concentric rings (plateau + smoothstep tail)
    _DISC_PLATEAU = 0.60            # inner fraction of the radius held at full weight
    _DISC_A = 80                    # peak gradient-descriptor alpha (RGB blit ignores it)

    def disc(self, color: tuple[int, int, int], radius: int) -> pygame.Surface:
        """Cached soft sov disc. LRU-bounded at ``_disc_max`` (MP6) -- a HIT is a
        dict get plus one O(1) ``move_to_end`` (measured: plain dict get 110 ns,
        OrderedDict get+move_to_end 255 ns, i.e. +146 ns/hit). ``_draw_sov`` calls
        this once per ON-SCREEN SOV SYSTEM (up to ~4,200), not once per distinct
        colour, so the worst frame pays ~4,200 hits -- ~0.6 ms against a 40-55 ms
        crisp render (~1%): hits stay cheap, dwarfed by MP8's saving. Eviction
        only ever runs on the build path. The returned Surface is the SAME object
        for a repeated key, exactly as before."""
        cache = self._disc_cache
        key = (color, radius)
        got = cache.get(key)
        if got is None:
            got = self._build_disc(color, radius)
            cache[key] = got
            while len(cache) > self._disc_max:
                cache.popitem(last=False)        # drop the least-recently-used
        else:
            cache.move_to_end(key)               # freshen (O(1) linked-list move)
        return got

    def _build_disc(self, color: tuple[int, int, int], radius: int) -> pygame.Surface:
        target = max(2 * radius, 2)
        ss = max(min(8 * radius, self._SS_MAX), target)   # supersample edge, >= target
        src = pygame.Surface((ss, ss), pygame.SRCALPHA)
        c = ss // 2
        outer = ss / 2.0
        plateau = self._DISC_PLATEAU
        n = self._DISC_N
        r0, g0, b0 = color
        for i in range(n):                                # rim(large) -> centre(small)
            u = i / (n - 1)
            rr = max(int(round(outer * (1.0 - u))), 1)
            t = 1.0 - u                                   # normalized radius: 1 rim, 0 centre
            if t <= plateau:
                w = 1.0                                   # flat plateau at full colour
            else:
                f = (t - plateau) / (1.0 - plateau)       # 0 at plateau edge, 1 at rim
                w = 1.0 - f * f * (3.0 - 2.0 * f)         # smoothstep 1 -> 0 (gentle both ends)
            col = (int(r0 * w), int(g0 * w), int(b0 * w), int(self._DISC_A * w))
            pygame.draw.circle(src, col, (c, c), rr)
        return pygame.transform.smoothscale(src, (target, target))


class LabelFactory:
    """Cached AA text surfaces keyed by (text, px, color, outline). font.init lazy.

    An ``outline`` colour (owner 2026-07-12 readability pass) bakes a 1px dark ring
    UNDER the glyphs so system names stay legible on any map background. The outlined
    composite is cached exactly like a plain label, so a caller that always passes the
    same outline pays the 2-render + 5-blit build ONCE per distinct (text, px, colour)
    -- the per-frame cost is a single blit of a prebuilt surface, ZERO delta vs the
    un-outlined label. Callers that omit ``outline`` (region + hub labels, infra chips)
    get the byte-identical plain render they always did.

    The surface cache is LRU-bounded (``LABEL_CACHE_MAX``, MP6) -- see that constant
    for the measured sizing. Nothing about a cache HIT changed: same key, same
    returned Surface object, one extra O(1) list-node move."""

    def __init__(self, max_entries: int = LABEL_CACHE_MAX) -> None:
        # _fonts is keyed by pixel size off the three band styles (13 / 15 / the
        # infra chip px) -- three entries, self-bounding, left a plain dict. The
        # SURFACE cache is the one that grew without limit (MP6): it is LRU-capped.
        self._fonts: dict[int, pygame.font.Font] = {}
        self._cache: "OrderedDict[tuple, pygame.Surface]" = OrderedDict()
        self._max_entries = max(1, int(max_entries))

    def _font(self, px: int) -> pygame.font.Font:
        f = self._fonts.get(px)
        if f is None:
            if not pygame.font.get_init():
                pygame.font.init()
            f = pygame.font.SysFont(_FONT_NAME, px)
            self._fonts[px] = f
        return f

    def label(self, text: str, px: int, color: tuple[int, int, int],
              outline: tuple[int, int, int] | None = None,
              glow: tuple[int, int, int] | None = None) -> pygame.Surface:
        """Cached (outlined, optionally glowing) text surface. LRU-bounded at
        ``_max_entries`` (MP6). The HIT path -- which runs per drawn label per frame
        on the render worker -- is still a single dict get, now plus one O(1)
        ``move_to_end``: measured 146 ns per hit (plain dict get 110 ns,
        OrderedDict get+move_to_end 255 ns), ~44 us across a full 300-label frame
        against a 40-55 ms render (~0.1%). A repeated key still returns the SAME
        Surface object, so every caller's blit is byte-identical to before.

        ``glow`` (owner 2026-08-08) paints a soft halo of that colour UNDER the
        glyphs -- the triple-overlap system-name treatment. It JOINS THE CACHE KEY,
        so the glowing and plain variants of one name are separate entries and a
        caller passing ``glow=None`` still gets the byte-identical surface it always
        did. Variant count is bounded and small: a band-C frame draws < 300 names,
        each in exactly ONE of five styles (LABEL_COLOR, the three single-effect
        tints, or the glow), so the worst frame's working set stays far under
        LABEL_CACHE_MAX = 1024 and the cache never thrashes.

        NOTE the glowing composite is LABEL_GLOW_PAD px larger on every side; the
        blit site must subtract that pad horizontally to keep the glyph where the
        un-glowing label would have sat (vertical centring absorbs it already)."""
        cache = self._cache
        key = (text, px, color, outline, glow)
        got = cache.get(key)
        if got is None:
            if glow is not None:
                got = self._render_glowing(text, px, color, outline, glow)
            elif outline is None:
                got = self._font(px).render(text, True, color)
            else:
                got = self._render_outlined(text, px, color, outline)
            cache[key] = got
            while len(cache) > self._max_entries:
                cache.popitem(last=False)        # drop the least-recently-used
        else:
            cache.move_to_end(key)               # freshen (O(1) linked-list move)
        return got

    def _render_outlined(self, text: str, px: int, color: tuple[int, int, int],
                         outline: tuple[int, int, int]) -> pygame.Surface:
        """Glyphs with a 1px dark outline baked in. Render the text once in the
        outline colour and blit it at the four cardinal +/-1px offsets, then the
        bright text on top (2 renders + 5 blits). The composite is 1px larger on
        every side (the ring), and the bright glyph lands at (1, 1) so its interior
        sits exactly where the un-outlined glyph would minus that 1px pad."""
        font = self._font(px)
        base = font.render(text, True, color)
        dark = font.render(text, True, outline)
        w, h = base.get_size()
        out = pygame.Surface((w + 2, h + 2), pygame.SRCALPHA)
        blit = out.blit
        for dx, dy in ((0, 1), (2, 1), (1, 0), (1, 2)):   # W, E, N, S of (1, 1)
            blit(dark, (dx, dy))
        blit(base, (1, 1))
        return out

    def _render_glowing(self, text: str, px: int, color: tuple[int, int, int],
                        outline: tuple[int, int, int] | None,
                        glow: tuple[int, int, int]) -> pygame.Surface:
        """Glyphs (outlined as usual) sitting on a soft coloured halo.

        The halo is a DILATION of the text: the same glyphs rendered in the glow
        hue and stamped at every offset on rings 1..LABEL_GLOW_PAD around the
        centre, each ring at its own alpha (LABEL_GLOW_RING_ALPHA, inner ->
        outer) so the halo falls off instead of reading as a hard slab. Two pygame
        mechanics make this exact rather than muddy:
          * per-ring alpha comes from ``fill(..., BLEND_RGBA_MULT)`` on a copy --
            it scales the per-pixel ALPHA and leaves RGB untouched (``set_alpha``
            is unreliable on a per-pixel-alpha font surface).
          * the stamps compose with ``BLEND_RGBA_MAX``, so overlapping offsets take
            the max instead of darkening each other the way an alpha-over blit onto
            a transparent surface would.
        The normal outlined glyph goes on LAST with a plain alpha blit, so the dark
        LABEL_OUTLINE ring still separates the bright text from its own halo.

        The composite is LABEL_GLOW_PAD px larger on every side than the outlined
        label; the glyph therefore sits at (pad, pad) and the caller compensates
        horizontally (see ``label``)."""
        pad = LABEL_GLOW_PAD
        core = (self._render_outlined(text, px, color, outline)
                if outline is not None else self._font(px).render(text, True, color))
        cw, ch = core.get_size()
        out = pygame.Surface((cw + 2 * pad, ch + 2 * pad), pygame.SRCALPHA)
        halo = self._font(px).render(text, True, glow)
        # Glyph origin INSIDE `core`: the outlined composite carries its own 1px
        # ring, so its glyphs start at (1, 1); a plain render starts at (0, 0).
        gx = gy = pad + (1 if outline is not None else 0)
        blit, mx = out.blit, pygame.BLEND_RGBA_MAX
        for ring, alpha in enumerate(LABEL_GLOW_RING_ALPHA[:pad], start=1):
            layer = halo.copy()
            layer.fill((255, 255, 255, alpha), None, pygame.BLEND_RGBA_MULT)
            for dx in range(-ring, ring + 1):
                for dy in range(-ring, ring + 1):
                    if max(abs(dx), abs(dy)) == ring:     # the ring, not its interior
                        blit(layer, (gx + dx, gy + dy), special_flags=mx)
        out.blit(core, (pad, pad))
        return out


# --- Tk hand-off -------------------------------------------------------------
def surface_to_ppm(surf: pygame.Surface) -> bytes:
    """P6 PPM bytes for tk.PhotoImage(data=...). ~5 ms at 1280x850 (measured).
    Accepts a subsurface (Task 17 center crop): pygame.image.tobytes walks the
    subsurface's region/pitch correctly, so no copy is needed."""
    w, h = surf.get_size()
    return b"P6\n%d %d\n255\n" % (w, h) + pygame.image.tobytes(surf, "RGB")


def center_subsurface(surf: pygame.Surface, margin: int, vw: int, vh: int) -> pygame.Surface:
    """Viewport-sized center crop of a MARGINED render (Task 17). Returns a
    subsurface VIEW that shares the parent's pixels (no copy); surface_to_ppm()
    reads it directly. Kept here so map_tab.py never imports pygame (module
    invariant: only map_render touches pygame). Use the result transiently -- a
    retained subsurface keeps the parent surface subsurface-locked."""
    return surf.subsurface(pygame.Rect(margin, margin, vw, vh))


# --- frame pipeline (spec §4.2 order) ---------------------------------------
def median_edge_length(model) -> float:
    """Median gate-edge length — robust vs long inter-region edges, which
    inflate the mean ~2x and (via the zoom ceiling) made band C unreachable
    in dense regions (Phase B checkpoint finding)."""
    if not model.edges:
        return 1.0
    lengths = []
    for a, b in model.edges:
        sa, sb = model.systems[a], model.systems[b]
        lengths.append(((sa.x - sb.x) ** 2 + (sa.y - sb.y) ** 2) ** 0.5)
    lengths.sort()
    return lengths[len(lengths) // 2]


class Renderer:
    """Turns (MapModel, Camera) into a finished glow frame. Stateless between
    frames except caches (sprites, labels, per-region nebula info)."""

    def __init__(self, model) -> None:
        self.model = model
        self.sprites = SpriteFactory()
        self.labels = LabelFactory()
        self._region_info = self._build_region_info()
        self._median_edge = median_edge_length(model)
        # Shared MAX-compose scratch: a plain-RGB surface the additive overlay
        # passes (sov Task 33; range-green + threat-purple, owner 2026-07-12)
        # MAX-compose their blobs onto before a SINGLE additive blit to the frame.
        # Lazily allocated and cached by frame size (reallocated only on a size
        # change) and reused SEQUENTIALLY within a frame -- each pass fills black,
        # MAX-composes, then blits before the next pass touches it -- so a steady
        # viewport reuses ONE buffer with no per-frame allocation churn. See
        # _get_scratch (the sole allocator).
        self._scratch: pygame.Surface | None = None
        self._scratch_size: tuple[int, int] | None = None
        # Task 18 Step 3 hot-loop caches (static; independent of camera/frame):
        #  * per-edge sec tint index -> edge loop drops per-frame sec_color()+max()
        #  * per-system (tint, is_hub) -> _draw_systems drops per-node sec_color()
        #    + HUB_IDS membership + the systems[sid] lookup
        #  * per-system world (x, y) -> the pos projection drops a dict lookup/node
        # Every cached value equals what the old code recomputed, so rendered bytes
        # are unchanged (determinism holds).
        systems = model.systems
        self._edge_sec_idx = [
            _sec_idx(max(systems[a].sec, systems[b].sec)) for a, b in model.edges]
        self._node_static = {
            sid: (_SEC_TINTS[_sec_idx(s.sec)], sid in HUB_IDS)
            for sid, s in systems.items()}
        self._node_xy = {sid: (s.x, s.y) for sid, s in systems.items()}

    def _build_region_info(self):
        """Per-region: (anchor_x, anchor_y, world_radius, tint) for the nebula.
        Also records self._region_size (member count) for label declutter priority."""
        by_region: dict[int, list] = {}
        for s in self.model.systems.values():
            by_region.setdefault(s.region_id, []).append(s)
        self._region_size = {rid: len(members) for rid, members in by_region.items()}
        info = []
        for rid, members in sorted(by_region.items()):
            anchor = self.model.region_anchors.get(rid)
            if anchor is None or not members:
                continue
            _, ax, ay = anchor
            r = max(max(((s.x - ax) ** 2 + (s.y - ay) ** 2) ** 0.5 for s in members), 1e-9)
            counts = {"H": 0, "L": 0, "N": 0}
            for s in members:
                counts["H" if s.sec >= 0.45 else "L" if s.sec > 0.0 else "N"] += 1
            tint = {"H": SEC_HI, "L": SEC_LOW, "N": SEC_NULL}[max(counts, key=counts.get)]
            info.append((ax, ay, r * 0.7, tint))
        return info

    # -- public ---------------------------------------------------------------
    def render(self, cam, vw: int, vh: int, *, bloom: bool = True,
               mode: str = "full", band: str | None = None,
               tint: TintSpec | None = None,
               bridges: tuple | None = None,
               heat: tuple | None = None,
               sov: tuple | None = None,
               infra: tuple | None = None) -> pygame.Surface:
        surf = pygame.Surface((vw, vh))
        surf.fill(BG)

        margin = 64.0
        x0, y0, x1, y1 = cam.visible_world_rect(vw, vh, margin_px=margin)
        visible = list(self.model.systems_in_rect(x0, y0, x1, y1))
        st = BAND_STYLES[band or pick_band(len(visible))]
        glow_r, core_r = node_metrics(cam.scale * self._median_edge)

        self._draw_nebula(surf, cam, vw, vh)
        # Project visible systems. Inlines world_to_screen (one dict lookup per
        # system via the static _node_xy cache; no per-call function overhead) --
        # the arithmetic is the SAME operations on the SAME float operands as
        # cam.world_to_screen, so the projected coords are bit-identical.
        cx, cy, scale = cam.cx, cam.cy, cam.scale
        hw, hh = vw / 2.0, vh / 2.0
        node_xy = self._node_xy
        pos = {}
        for sid in visible:
            wx, wy = node_xy[sid]
            pos[sid] = ((wx - cx) * scale + hw, (wy - cy) * scale + hh)
        vis_set = set(visible)

        # Sovereignty tint (Task 33): the DEEPEST overlay wash -- drawn before
        # edges / bridges / heat / nodes so the whole glowing map sits on top of
        # it. Same truthiness gate as bridges/heat: sov=None/() runs the
        # pre-change path exactly, keeping frames byte-identical (determinism).
        if sov:
            self._draw_sov(surf, cam, pos, vis_set, vw, vh, sov)

        if mode == "degraded":
            self._draw_edges_degraded(surf, cam, vw, vh, pos, vis_set)
        else:
            self._draw_edges(surf, st, pos, vis_set, cam, vw, vh)
        # Ansiblex bridges: after gate edges, before node glows (so systems stay
        # readable). Gated on truthiness -> bridges=None/() runs the pre-change
        # path exactly, keeping bytes byte-identical (determinism).
        if bridges:
            self._draw_bridges(surf, cam, pos, vis_set, vw, vh, bridges)
        # Kill-heat under-glow (Task 30): after bridges, before node glows, so the
        # red-orange heat sits under the system cores. Same truthiness gate ->
        # heat=None/() is byte-identical to the pre-heat frame (determinism). The
        # pass is bloom-independent -- it runs whether or not the bloom pass fires.
        if heat:
            self._draw_heat(surf, cam, pos, vis_set, vw, vh, heat)
        self._draw_systems(surf, st, pos, glow_r, core_r, tint)
        if bloom and mode != "degraded":
            _bloom_pass(surf)
        # Infra count chips ride the label pass (same st.system_labels zoom LOD);
        # infra=None/() draws nothing, keeping the frame byte-identical to the
        # pre-infra output (determinism, exactly like bridges/heat/sov).
        self._draw_labels(surf, st, pos, cam, vw, vh, infra, tint, glow_r)
        return surf

    # -- passes ----------------------------------------------------------------
    def _draw_nebula(self, surf, cam, vw, vh):
        for ax, ay, wr, tint in self._region_info:
            sx, sy = cam.world_to_screen(ax, ay, vw, vh)
            r_px = wr * cam.scale
            if r_px < 24 or sx < -r_px or sy < -r_px or sx > vw + r_px or sy > vh + r_px:
                continue
            bucket = min(int(r_px / 48) * 48 + 48, 480)
            sprite = self.sprites.glow(dim(tint, NEBULA_DIM), bucket // 2)
            surf.blit(sprite, (sx - sprite.get_width() / 2, sy - sprite.get_height() / 2),
                      special_flags=pygame.BLEND_RGB_ADD)

    def _edge_endpoints(self, pos, vis_set):
        """Yield (sec_idx, a, b, pa, pb, sa, sb) for every edge with a visible
        endpoint. sec_idx is the precomputed tint index (Task 18 Step 3); pa/pb
        are the cached projections (None => off-visible, the caller re-projects)."""
        systems = self.model.systems
        sec_idx = self._edge_sec_idx
        get = pos.get
        for i, (a, b) in enumerate(self.model.edges):
            if a in vis_set or b in vis_set:
                yield sec_idx[i], a, b, get(a), get(b), systems[a], systems[b]

    def _draw_edges(self, surf, st, pos, vis_set, cam, vw, vh):
        # Only 3 possible edge tints -> derive the two dimmed colours (wide pass +
        # aaline pass) ONCE per frame; the per-edge loop just indexes them by the
        # precomputed sec index. Byte-identical to the old per-edge sec_color/dim.
        ew = st.edge_width
        wide = tuple(dim(t, 0.25) for t in _SEC_TINTS)
        line = tuple(dim(t, st.edge_dim) for t in _SEC_TINTS)
        draw_line, draw_aaline = pygame.draw.line, pygame.draw.aaline
        for idx, a, b, pa, pb, sa, sb in self._edge_endpoints(pos, vis_set):
            if pa is None:
                pa = cam.world_to_screen(sa.x, sa.y, vw, vh)
            if pb is None:
                pb = cam.world_to_screen(sb.x, sb.y, vw, vh)
            if ew:
                draw_line(surf, wide[idx], pa, pb, ew)
            draw_aaline(surf, line[idx], pa, pb)

    def _draw_edges_degraded(self, surf, cam, vw, vh, pos, vis_set):
        """Fast path (spec §4.3): crisp 1px edge layer + bloom of that layer only."""
        layer = pygame.Surface(surf.get_size())
        layer.fill((0, 0, 0))
        line6 = tuple(dim(t, 0.6) for t in _SEC_TINTS)   # 3 dimmed tints, once/frame
        draw_line = pygame.draw.line
        for idx, a, b, pa, pb, sa, sb in self._edge_endpoints(pos, vis_set):
            if pa is None:
                pa = cam.world_to_screen(sa.x, sa.y, vw, vh)
            if pb is None:
                pb = cam.world_to_screen(sb.x, sb.y, vw, vh)
            draw_line(layer, line6[idx], pa, pb, 1)
        _bloom_pass(layer)
        surf.blit(layer, (0, 0), special_flags=pygame.BLEND_RGB_ADD)

    def _draw_bridges(self, surf, cam, pos, vis_set, vw, vh, bridges):
        """Draw each friendly Ansiblex connection as a glowing blue line (owner
        request). `bridges` is a tuple of unordered (id_a, id_b) system-id pairs
        (map_overlays.resolve_bridges). Sorted iteration keeps the draw
        deterministic regardless of input order. Endpoints reuse the cached
        projection when visible (like _draw_edges) and re-project otherwise; a
        bridge is drawn when either endpoint is in the visible set OR its
        projected segment's bbox overlaps the surface (long cross-map bridges
        whose endpoints are both off-view but which cross the frame). Three
        passes -- wide dim / mid / bright aaline -- build the glow; all sit UNDER
        the node glows drawn next, so systems stay readable."""
        systems = self.model.systems
        get = pos.get
        wide = dim(BRIDGE_BLUE, 0.30)
        mid = dim(BRIDGE_BLUE, 0.55)
        bright = dim(BRIDGE_BLUE, 0.95)
        draw_line, draw_aaline = pygame.draw.line, pygame.draw.aaline
        for a, b in sorted(bridges):
            sa = systems.get(a)
            sb = systems.get(b)
            if sa is None or sb is None:
                continue
            pa = get(a)
            if pa is None:
                pa = cam.world_to_screen(sa.x, sa.y, vw, vh)
            pb = get(b)
            if pb is None:
                pb = cam.world_to_screen(sb.x, sb.y, vw, vh)
            if not (a in vis_set or b in vis_set
                    or _segment_on_surface(pa, pb, vw, vh)):
                continue
            draw_line(surf, wide, pa, pb, 4)
            draw_line(surf, mid, pa, pb, 2)
            draw_aaline(surf, bright, pa, pb)

    def _get_scratch(self, size):
        """Return the shared plain-RGB MAX-compose scratch, (re)allocated only on a
        frame-size change. Reused SEQUENTIALLY within a frame by the sov /
        range-green / threat-purple / friendly-blue / overlap-split passes: each
        fills it black, BLEND_RGB_MAX-
        composes its blobs, then does ONE BLEND_RGB_ADD blit to the frame before the
        next pass touches it -- so a steady viewport reuses one buffer with no
        per-frame allocation churn. The sole allocator of the scratch."""
        scratch = self._scratch
        if scratch is None or self._scratch_size != size:
            scratch = self._scratch = pygame.Surface(size)   # plain RGB
            self._scratch_size = size
        return scratch

    def _draw_sov(self, surf, cam, pos, vis_set, vw, vh, sov):
        """Sovereignty tint under-wash (Task 33). `sov` is an iterable of
        ``(system_id, alliance_id)`` pairs (the canonical request tuple). Each
        sov'd system gets ONE soft disc in its alliance's hashed color
        (map_overlays.sov_color) at the FIXED ``SOV_RADIUS`` -- comfortably larger
        than the node glows -- so adjacent same-alliance systems merge into a soft
        regional wash. Drawn FIRST (under heat / bridges / nodes) so the glowing
        node cores stay readable on top. Endpoints reuse the cached projection
        when visible and re-project otherwise; off-surface systems are culled.

        COMPOSE (the white-out fix): the discs are NOT ADD-blitted onto the frame
        (that summed overlapping blobs toward white in dense single-alliance
        regions -- measured (245,245,235) with all of Delve on one alliance at
        fit-universe). Instead every disc is composited per-pixel with
        ``BLEND_RGB_MAX`` onto a black scratch surface, then the scratch is blitted
        ONCE onto the frame with ``BLEND_RGB_ADD``. Guarantees:
          * SAME-alliance overlap can never exceed a single disc's peak per channel
            -- ``max(a, a) = a`` -- so a dense region becomes a FLAT wash at the
            sprite brightness (~102/255), the original "adjacent systems merge into
            a region wash" intent, and can NEVER brighten toward white.
          * DIFFERENT-alliance borders take the per-channel max of the two colors
            (a mild hue blend, no brightening beyond either color).
        Disc sprites cache by (color, radius) in the SpriteFactory; radius is
        FIXED, so the cache is bounded by the alliance count. A per-frame memo
        avoids re-hashing a color when a whole null bloc shares one alliance. The
        scratch is cached by frame size (reallocated only on a size change).
        Byte-behaviour matches heat/bridges: the caller gates on truthiness, so
        sov=None/() never calls this (frame stays byte-identical to pre-sov)."""
        systems = self.model.systems
        get = pos.get
        size = surf.get_size()
        scratch = self._get_scratch(size)
        scratch.fill((0, 0, 0))
        disc, blit_max = self.sprites.disc, pygame.BLEND_RGB_MAX
        blit_s = scratch.blit
        sov_color = mo.sov_color
        color_memo: dict[int, tuple[int, int, int]] = {}
        for sid, aid in sov:
            s = systems.get(sid)
            if s is None:
                continue
            p = get(sid)
            if p is None:
                p = cam.world_to_screen(s.x, s.y, vw, vh)
            sx, sy = p
            if (sx < -SOV_RADIUS or sy < -SOV_RADIUS
                    or sx > vw + SOV_RADIUS or sy > vh + SOV_RADIUS):
                continue
            color = color_memo.get(aid)
            if color is None:
                color = color_memo[aid] = sov_color(aid)
            g = disc(color, SOV_RADIUS)
            blit_s(g, (sx - g.get_width() / 2, sy - g.get_height() / 2),
                   special_flags=blit_max)
        surf.blit(scratch, (0, 0), special_flags=pygame.BLEND_RGB_ADD)

    def _draw_range_glow(self, surf, pos, bright, glow_r, skip=frozenset()):
        """In-range GREEN accent (owner 2026-07-12): the readability fix for the old
        red-on-red range overlay. Every in-range system (``TintSpec.bright``) gets
        ONE soft green glow at ``glow_r + RANGE_GLOW_PAD`` -- a tight halo that hugs
        the node so the aura just peeks out AROUND it -- MAX-composed onto the shared
        scratch, then blitted ONCE with BLEND_RGB_ADD (the _draw_sov house pattern).
        So a dense in-range cluster washes FLAT green (per-pixel max(a, a) = a) and
        can NEVER brighten toward white, while a lone in-range system reads as a clear
        green halo even against red nullsec cores (#cc2233). The sprite colour is
        ``RANGE_GREEN_GLOW`` -- RANGE_GREEN dimmed to ~57% (owner readability pass) so
        the aura no longer floods over the system-name labels; the RGB is scaled
        BEFORE the build so the whole gradient + its MAX ceiling drop together, hue
        intact. Drawn UNDER the node glows (called from _draw_systems before its
        per-node loop) so each system's own colour + white core stay bright on top --
        the green is the accent, not a recolour. The sprite is identical for every
        in-range system, so it is built once; only currently-projected systems are
        in ``pos``, so off-screen in-range systems cost nothing.

        ``skip`` (owner 2026-08-08) holds the sids whose green has moved onto the
        split overlap blob -- drawn as its RIGHT half for class "TR", or dropped
        entirely for the triple "TFR" (where the jump-range signal lives on the
        label's yellow glow instead). Those systems must NOT also take the full
        accent, or the semicircle would sit inside a complete green ring."""
        size = surf.get_size()
        scratch = self._get_scratch(size)
        scratch.fill((0, 0, 0))
        g = self.sprites.glow(RANGE_GREEN_GLOW, glow_r + RANGE_GLOW_PAD)
        off = g.get_width() / 2.0
        blit_s, blit_max = scratch.blit, pygame.BLEND_RGB_MAX
        for sid, (sx, sy) in pos.items():
            if sid in bright and sid not in skip:
                blit_s(g, (sx - off, sy - off), special_flags=blit_max)
        surf.blit(scratch, (0, 0), special_flags=pygame.BLEND_RGB_ADD)

    def _draw_threat_glow(self, surf, pos, halo, glow_r, skip=frozenset()):
        """Threat PURPLE under-wash (owner 2026-07-12; replaces the old per-node RED
        additive halo). Every threatened system (``TintSpec.halo``) gets ONE dim
        violet glow at ``glow_r + WASH_GLOW_PAD``, MAX-composed onto the shared
        scratch then blitted ONCE with BLEND_RGB_ADD -- so threat blobs top out FLAT
        (never white), exactly like sov/range. Violet (#8e5bd6) reads as a distinct
        background warning wash vs red nullsec nodes, blue Ansiblex bridges, green
        range, and amber intel -- the owner's "hard to distinguish" red is gone.
        Drawn UNDER the node glows (from _draw_systems before its per-node loop) so
        the cores stay readable on top.

        NOTE: hostile-staging DIAMONDS stay RED (map_tab._draw_diamond, ov_staging)
        -- those are POINT markers of a DIFFERENT semantic (where the hostiles ARE,
        not their projected jump reach), so their red is intentional and untouched
        here. This purple wash is only the projected-threat sphere.

        ``skip`` (owner 2026-08-08) holds every sid drawn as a SPLIT overlap blob.
        Each split class contains T, so the threat pass skips the whole split set:
        the purple is still there, as the blob's LEFT half."""
        size = surf.get_size()
        scratch = self._get_scratch(size)
        scratch.fill((0, 0, 0))
        g = self.sprites.glow(THREAT_PURPLE, glow_r + WASH_GLOW_PAD)
        off = g.get_width() / 2.0
        blit_s, blit_max = scratch.blit, pygame.BLEND_RGB_MAX
        for sid, (sx, sy) in pos.items():
            if sid in halo and sid not in skip:
                blit_s(g, (sx - off, sy - off), special_flags=blit_max)
        surf.blit(scratch, (0, 0), special_flags=pygame.BLEND_RGB_ADD)

    def _draw_friendly_glow(self, surf, pos, friendly, glow_r, skip=frozenset()):
        """Friendly-staging PROJECTION wash (owner ask): a SECOND halo beside the
        hostile purple threat, in deep azure BLUE (FRIENDLY_BLUE #3d7dd6). Every
        system inside a FRIENDLY staging's jump/bridge reach (``TintSpec.friendly``)
        gets ONE dim blue glow at ``glow_r + WASH_GLOW_PAD``, MAX-composed onto the
        shared scratch then blitted ONCE with BLEND_RGB_ADD -- so friendly blobs top out
        FLAT (never white), byte-for-byte the same compose as _draw_threat_glow, just
        a different hue. Blue reads distinct from the purple hostile wash (its GREEN
        channel sits above red, purple's RED sits above green), the green range aura,
        and the brighter blue Ansiblex bridge LINES. Where a system is in BOTH the
        hostile and friendly reach the two washes ADD to a saturated blue-violet whose
        R/G stay well under white (both peak the BLUE channel to clamp; the MAX-then-add
        compose was chosen so the overlap never whitens). Drawn UNDER the node glows
        (from _draw_systems before its per-node loop, ADJACENT to the threat pass at the
        same layer depth) so cores stay readable on top. Sequential scratch reuse: the
        threat pass already ADD-blitted its scratch to the frame before this fills black
        again (the _draw_sov / _draw_range_glow / _draw_threat_glow house pattern).

        ``skip`` (owner 2026-08-08) holds the sids whose blue has moved onto the split
        overlap blob as its RIGHT half (classes "TF" and "TFR"); they must not also
        take the full wash. A friendly system with NO threat is never in ``skip`` --
        including class "FR" (friendly + range), which is deliberately left alone."""
        size = surf.get_size()
        scratch = self._get_scratch(size)
        scratch.fill((0, 0, 0))
        g = self.sprites.glow(FRIENDLY_BLUE, glow_r + WASH_GLOW_PAD)
        off = g.get_width() / 2.0
        blit_s, blit_max = scratch.blit, pygame.BLEND_RGB_MAX
        for sid, (sx, sy) in pos.items():
            if sid in friendly and sid not in skip:
                blit_s(g, (sx - off, sy - off), special_flags=blit_max)
        surf.blit(scratch, (0, 0), special_flags=pygame.BLEND_RGB_ADD)

    def _split_map(self, pos, bright, halo, friendly) -> dict:
        """``{sid: effect_class}`` for the PROJECTED systems that need a split blob.

        Every split class carries T, so the candidate set is the threat set --
        iterate THAT (bounded by the hostile projection), never the whole projected
        map, which can hold thousands of systems at universe zoom. Returns {} the
        moment a split is impossible (no threat, or no second effect), so a
        single-layer frame pays one truthiness check."""
        if not halo or not (bright or friendly):
            return {}
        out = {}
        for sid in halo:
            if sid in pos:
                cls = classify_effects(sid, bright, halo, friendly)
                if cls in SPLIT_CLASSES:
                    out[sid] = cls
        return out

    def _draw_split_glow(self, surf, pos, splits, glow_r):
        """The overlap SEMICIRCLE pass (owner 2026-08-08). ``splits`` is
        ``{sid: effect_class}`` from ``_split_map`` -- systems covered by the hostile
        threat AND at least one other effect, which used to ADD into one ambiguous
        blob. Each gets ONE disc at the SAME wash radius (``glow_r +
        WASH_GLOW_PAD``) as the full washes it replaces, cut down its vertical
        diameter: LEFT half THREAT_PURPLE (always -- left is hostile, a fixed
        convention so the map is readable without a legend), RIGHT half whatever
        ``split_right_color`` says (friendly blue, else the dimmed range green).

        The two halves are complementary column ranges of the same sprite, so they
        MAX-compose into ONE seamless disc -- no gap column, no doubled column, same
        radial falloff as a full wash. House compose rule holds: fill the shared
        scratch black, BLEND_RGB_MAX every half onto it, then ONE BLEND_RGB_ADD blit
        to the frame -- so any density of overlapping split blobs tops out FLAT and
        can never brighten toward white. Runs LAST of the four tint passes, adjacent
        to threat/friendly at the same layer depth, and the systems it draws are
        excluded from those passes (see their ``skip`` arguments)."""
        size = surf.get_size()
        scratch = self._get_scratch(size)
        scratch.fill((0, 0, 0))
        half = self.sprites.half_glow
        radius = glow_r + WASH_GLOW_PAD
        left = half(THREAT_PURPLE, radius, "L")
        off = left.get_width() / 2.0
        blit_s, blit_max = scratch.blit, pygame.BLEND_RGB_MAX
        right_by_class: dict[str, pygame.Surface] = {}
        # Iterate the SPLITS, not pos: _split_map only admits sids it found in pos,
        # so pos[sid] is a guaranteed hit -- and splits is bounded by the threat
        # projection while pos can hold every visible system (~5,485 at universe
        # zoom, measured 243 us/frame when this loop scanned pos instead).
        for sid, cls in splits.items():
            sx, sy = pos[sid]
            right = right_by_class.get(cls)
            if right is None:
                right = right_by_class[cls] = half(split_right_color(cls),
                                                   radius, "R")
            blit_s(left, (sx - off, sy - off), special_flags=blit_max)
            blit_s(right, (sx - off, sy - off), special_flags=blit_max)
        surf.blit(scratch, (0, 0), special_flags=pygame.BLEND_RGB_ADD)

    def _draw_heat(self, surf, cam, pos, vis_set, vw, vh, heat):
        """Kill-heat under-glow (Task 30). `heat` is an iterable of
        ``(system_id, intensity)`` pairs (the canonical request tuple;
        intensity 0..1). Each hot system gets ONE additive red-orange glow sprite
        whose radius scales with intensity (``8 + 14 * heat`` px -- the same
        node_metrics-style soft growth the node glows use), drawn under the node
        glows so cores stay readable. Endpoints reuse the cached projection when
        visible and re-project otherwise; off-surface systems are culled. Sprites
        are cached by (color, radius) in the shared SpriteFactory, so repeated
        intensities are free. Byte-behaviour matches bridges: the caller gates on
        truthiness, so heat=None/() never calls this."""
        systems = self.model.systems
        get = pos.get
        glow, blit, add = self.sprites.glow, surf.blit, pygame.BLEND_RGB_ADD
        for sid, h in heat:
            if h <= 0.0:
                continue
            s = systems.get(sid)
            if s is None:
                continue
            p = get(sid)
            if p is None:
                p = cam.world_to_screen(s.x, s.y, vw, vh)
            sx, sy = p
            if sx < -60 or sy < -60 or sx > vw + 60 or sy > vh + 60:
                continue
            radius = int(8 + 14 * h)
            g = glow(HEAT_COLOR, radius)
            blit(g, (sx - g.get_width() / 2, sy - g.get_height() / 2),
                 special_flags=add)

    def _draw_systems(self, surf, st, pos, glow_r, core_r, tint=None):
        # Per-node colour + hub flag are STATIC -> read them from _node_static
        # instead of recomputing sec_color()/HUB_IDS membership every frame.
        # Loop-invariant hub radius and ring gate are hoisted; local aliases cut
        # attribute lookups. Rendered bytes are unchanged (same colours/radii).
        node_static = self._node_static
        bright = tint.bright if tint is not None else None
        halo = tint.halo if tint is not None else None
        friendly = tint.friendly if tint is not None else None
        # In-range GREEN accent + hostile-threat PURPLE wash + friendly-projection
        # BLUE wash (owner asks): each is a MAX-composed under-glow drawn BEFORE the
        # per-node loop, so the node cores stay bright ON TOP and each pass tops out
        # FLAT (never washes toward white). All gate on `is not None`, so an INACTIVE
        # layer runs the pre-change path byte-identically (like sov/heat/bridges) --
        # the pass simply never fires. The friendly pass sits ADJACENT to the threat
        # pass at the same layer depth; where both fire, purple + blue ADD to a
        # readable blue-violet, never white. See _draw_range_glow / _draw_threat_glow
        # / _draw_friendly_glow.
        #
        # OVERLAP (owner 2026-08-08): a system covered by the threat AND another
        # effect is drawn ONCE as a split disc by _draw_split_glow instead of taking
        # two full washes that ADD into an ambiguous blob. Each such system is
        # removed from every full pass whose colour its blob already carries -- the
        # threat pass always (every split class has T), friendly when the class has
        # F, range when the class has R (drawn as the right half for "TR",
        # suppressed for the triple "TFR"). "FR" -- friendly + range, no threat --
        # is NOT a split class, so it runs both original passes unchanged.
        splits = self._split_map(pos, bright, halo, friendly)
        skip_t = skip_f = skip_r = frozenset()
        if splits:
            skip_t = frozenset(splits)                      # every split class has T
            skip_f = frozenset(s for s, c in splits.items() if "F" in c)
            skip_r = frozenset(s for s, c in splits.items() if "R" in c)
        if bright is not None:
            self._draw_range_glow(surf, pos, bright, glow_r, skip_r)
        if halo is not None:
            self._draw_threat_glow(surf, pos, halo, glow_r, skip_t)
        if friendly is not None:
            self._draw_friendly_glow(surf, pos, friendly, glow_r, skip_f)
        if splits:
            self._draw_split_glow(surf, pos, splits, glow_r)
        hub_r = glow_r + max(3, glow_r // 3)        # was a flat +6; scales with zoom
        ring = st.core_ring and core_r >= 2         # ring around a 1px core = blob
        glow, blit, add = self.sprites.glow, surf.blit, pygame.BLEND_RGB_ADD
        for sid, (sx, sy) in pos.items():
            color, is_hub = node_static[sid]
            dimmed = bright is not None and sid not in bright
            draw_color = dim(color, 0.35) if dimmed else color
            radius = hub_r if is_hub else glow_r
            g = glow(draw_color, radius)
            blit(g, (sx - g.get_width() / 2, sy - g.get_height() / 2),
                 special_flags=add)
            if dimmed:
                continue                            # no core/ring on dimmed systems
            gfx.filled_circle(surf, int(sx), int(sy), core_r, (255, 255, 255))
            if ring:
                gfx.aacircle(surf, int(sx), int(sy), core_r, color)

    def _draw_labels(self, surf, st, pos, cam, vw, vh, infra=None, tint=None,
                     glow_r=0):
        if st.system_labels:
            # Effect-tinted names (owner 2026-08-08). A name is tinted only when
            # EXACTLY ONE range effect covers its system, and glows yellow only on
            # the triple -- label_style owns that rule. Gate the whole thing on an
            # active tint so a no-overlay frame pays ZERO classification cost and
            # every name keeps LABEL_COLOR. (That gate is about the tint work only:
            # the same readability pass moved the label OFFSET for every frame,
            # overlay or not, so this branch is NOT byte-identical to the
            # pre-2026-08-08 renderer -- unlike the region/hub branch below.)
            bright = halo = friendly = None
            if tint is not None:
                bright, halo, friendly = tint.bright, tint.halo, tint.friendly
            tinted = not (bright is None and halo is None and friendly is None)
            # Zoom-aware: the accent this offset has to clear grows with glow_r.
            offset_x = label_offset_x(glow_r)
            # Priority: hubs first, then alphabetical; occupancy grid drops overlaps.
            order = sorted(pos, key=lambda sid: (sid not in HUB_IDS,
                                                 self.model.systems[sid].name))
            items = [(sid, pos[sid][0], pos[sid][1]) for sid in order]
            for sid, sx, sy in _declutter(items, 96, 24):
                # Dark 1px outline baked in (owner 2026-07-12): system names must read
                # over the green range wash / purple threat / sov tint / nebula. The
                # outlined surface is cached, so this stays one blit of a prebuilt
                # sprite -- zero per-frame cost delta. Region + hub labels (else branch
                # below) pass no outline, so the zoomed-out bands are byte-unchanged.
                color, glow = LABEL_COLOR, None
                if tinted:
                    color, glow = label_style(
                        classify_effects(sid, bright, halo, friendly))
                lab = self.labels.label(self.model.systems[sid].name, st.label_px,
                                        color, outline=LABEL_OUTLINE, glow=glow)
                # offset_x clears the node core + its range accent at THIS zoom
                # (owner 2026-08-08). The glowing composite is LABEL_GLOW_PAD wider
                # on every side, so back the blit off by that pad to leave the GLYPH
                # exactly where an un-glowing label's would sit; the vertical
                # centring below absorbs the pad on its own.
                dx = offset_x - (LABEL_GLOW_PAD if glow is not None else 0)
                surf.blit(lab, (sx + dx, sy - lab.get_height() / 2))
            # Infra chips share this zoom LOD (Task 5): drawn AFTER labels so a
            # badge sits over its system's label when they overlap.
            if infra:
                self._draw_infra_chips(surf, pos, infra)
        else:
            # Region labels: biggest regions win the cell; overlapping small ones drop.
            region_items = []
            for rid, (name, ax, ay) in self.model.region_anchors.items():
                sx, sy = cam.world_to_screen(ax, ay, vw, vh)
                if -100 <= sx <= vw + 100 and -40 <= sy <= vh + 40:
                    region_items.append((rid, sx, sy))
            region_items.sort(key=lambda t: self._region_size.get(t[0], 0),
                              reverse=True)
            for rid, sx, sy in _declutter(region_items, 110, 26):
                lab = self.labels.label(self.model.region_anchors[rid][0], 15,
                                        REGION_LABEL_COLOR)
                surf.blit(lab, (sx - lab.get_width() / 2, sy - lab.get_height() / 2))
            if st.hub_labels:
                for sid in pos:
                    if sid in HUB_IDS:
                        sx, sy = pos[sid]
                        lab = self.labels.label(self.model.systems[sid].name,
                                                st.label_px, LABEL_COLOR)
                        # Band-M hub names keep the original +7: this band draws no
                        # per-system labels, so nothing crowds them, and the owner's
                        # 2026-08-08 offset ask is scoped to the system-label band.
                        surf.blit(lab, (sx + 7, sy - lab.get_height() / 2))

    def _draw_infra_chips(self, surf, pos, infra):
        """Per-system infrastructure count chips (Task 5). ``infra`` is the render
        request tuple ``((system_id, total, top_category, stale), ...)`` -- the
        SAME value that joins _request_sig, so the drawn frame matches the sig.
        Only systems in ``pos`` (currently projected / visible) get a chip; the
        count text reuses the cached label font (one Surface per distinct
        text+color -- NO per-chip font work, so a few hundred chips add no
        measurable frame cost). Called ONLY from the st.system_labels branch, so
        chips share the system-label zoom LOD (no new heuristic). Each chip is a
        rounded rect tinted by the dominant category with the count centered;
        stale systems dim the fill by INFRA_CHIP_STALE_DIM, and the text color
        auto-picks dark/light by the (possibly dimmed) fill luminance so it stays
        legible on every tint and in both states."""
        get = pos.get
        label = self.labels.label
        draw_rect = pygame.draw.rect
        chip_colors = INFRA_CHIP_COLORS
        unknown = chip_colors["unknown"]
        for sid, total, top, stale in infra:
            p = get(sid)
            if p is None:
                continue                       # off-screen system -> no chip
            fill = chip_colors.get(top, unknown)
            if stale:
                fill = dim(fill, INFRA_CHIP_STALE_DIM)
            # Perceptual luminance -> dark number on light chips, light on dark.
            lum = 0.299 * fill[0] + 0.587 * fill[1] + 0.114 * fill[2]
            text_color = INFRA_CHIP_TEXT_DARK if lum >= 140 else INFRA_CHIP_TEXT_LIGHT
            txt = label(str(total), INFRA_CHIP_PX, text_color)
            tw, th = txt.get_width(), txt.get_height()
            cw, ch = tw + 8, th + 4          # 4px horiz / 2px vert padding
            # Fixed (+7, -9) px offset from the node dot, NOT scaled by zoom (the
            # name label doesn't scale its offset either); the chip's left edge sits
            # at +7 and its vertical center at -9. Deliberately NOT moved with
            # LABEL_OFFSET_X (owner 2026-08-08): the chip rides ABOVE the name and
            # its own opaque plate carries its contrast, so the readability ask that
            # pushed names out to +11 does not apply to it.
            cx = int(p[0]) + 7
            cy = int(p[1]) - 9
            draw_rect(surf, fill, pygame.Rect(cx, cy - ch // 2, cw, ch),
                      border_radius=ch // 2)
            surf.blit(txt, (cx + 4, cy - th // 2))


def _segment_on_surface(pa, pb, vw: int, vh: int) -> bool:
    """Cheap bbox overlap between a screen segment and the [0,vw]x[0,vh] surface
    -- the bridge-cull "plausibly on-surface" test for long cross-map bridges
    whose endpoints are both outside the visible node set. Conservative (a
    diagonal whose bbox overlaps but which itself misses is still drawn), but
    pygame clips the line, so an occasional wasted draw is harmless."""
    minx, maxx = (pa[0], pb[0]) if pa[0] <= pb[0] else (pb[0], pa[0])
    miny, maxy = (pa[1], pb[1]) if pa[1] <= pb[1] else (pb[1], pa[1])
    return not (maxx < 0 or minx > vw or maxy < 0 or miny > vh)


def _bloom_pass(surf: pygame.Surface) -> None:
    w, h = surf.get_size()
    small = pygame.transform.smoothscale(surf, (max(w // 4, 1), max(h // 4, 1)))
    big = pygame.transform.smoothscale(small, (w, h))
    surf.blit(big, (0, 0), special_flags=pygame.BLEND_RGB_ADD)


# --- gesture frame cache (slippy-map zoom) -----------------------------------
class FrameCache:
    """Holds the last crisp frame + its camera; quick_frame() derives a gesture
    frame by crop+smoothscale (round-2 benchmark: ~36 ms at 1280x850) so zoom
    feels continuous while the worker renders the crisp frame in background.

    The stored surface may be LARGER than the viewport (Task 17: the worker
    renders a MARGIN border on every side so pan / zoom-out serve real content
    instead of a black edge). store() records the SURFACE dims; the source-rect
    math works in world space against the cached camera + those surface dims, so
    a viewport smaller than the surface is normal -- not a bail."""

    def __init__(self) -> None:
        self._surf: pygame.Surface | None = None
        self._cx = 0.0
        self._cy = 0.0
        self._scale = 1.0
        self._vw = 0
        self._vh = 0

    def store(self, surf: pygame.Surface, cam, view_vw: int, view_vh: int) -> None:
        """Cache the crisp frame + camera. `surf` may be MARGINED (larger than
        the viewport); the source-rect math needs the SURFACE dims, so record
        those from surf.get_size(). `view_vw`/`view_vh` are the viewport dims the
        caller displays -- informational (the surface is centered on the same
        camera with margin on every side); they are no longer used for a
        size-match bail (Task 17)."""
        self._surf = surf
        self._cx, self._cy, self._scale = cam.cx, cam.cy, cam.scale
        self._vw, self._vh = surf.get_size()

    def clear(self) -> None:
        self._surf = None

    def quick_frame(self, cam, vw: int, vh: int) -> pygame.Surface | None:
        if self._surf is None:          # a viewport != surface dims is normal (margin)
            return None
        ratio = cam.scale / self._scale
        # Wanted viewport corners in CACHED-frame pixel coordinates:
        #   cached_px = (world - cached_c) * cached_scale + v/2
        # where world spans the wanted rect derived from cam.
        wx0 = cam.cx - (vw / 2.0) / cam.scale
        wy0 = cam.cy - (vh / 2.0) / cam.scale
        src_x = (wx0 - self._cx) * self._scale + self._vw / 2.0
        src_y = (wy0 - self._cy) * self._scale + self._vh / 2.0
        src_w = vw / ratio
        src_h = vh / ratio

        out = pygame.Surface((vw, vh))
        out.fill(BG)
        # Intersect the wanted source rect with the cached surface:
        ix0, iy0 = max(src_x, 0.0), max(src_y, 0.0)
        ix1, iy1 = min(src_x + src_w, float(self._vw)), min(src_y + src_h, float(self._vh))
        if ix1 - ix0 < 1.0 or iy1 - iy0 < 1.0:
            return out                                   # fully off-cache: BG
        sub = self._surf.subsurface(
            pygame.Rect(int(ix0), int(iy0),
                        max(int(ix1 - ix0), 1), max(int(iy1 - iy0), 1)))
        dst_x = (ix0 - src_x) * ratio
        dst_y = (iy0 - src_y) * ratio
        dst_w = max(int((ix1 - ix0) * ratio), 1)
        dst_h = max(int((iy1 - iy0) * ratio), 1)
        out.blit(pygame.transform.smoothscale(sub, (dst_w, dst_h)),
                 (int(dst_x), int(dst_y)))
        return out


class SettleStats:
    """Rolling settle-render timings; suggests degraded mode when p90 exceeds
    threshold (spec §8: auto-degrade on weak machines)."""

    def __init__(self, threshold_ms: float = 250.0, window: int = 20) -> None:
        self.threshold_ms = threshold_ms
        self._times: list[float] = []
        self._window = window

    def record(self, ms: float) -> None:
        self._times.append(ms)
        if len(self._times) > self._window:
            self._times.pop(0)

    def suggest_mode(self) -> str:
        if len(self._times) < self._window // 2:
            return "full"
        ordered = sorted(self._times)
        p90 = ordered[min(int(len(ordered) * 0.9), len(ordered) - 1)]
        return "degraded" if p90 > self.threshold_ms else "full"
