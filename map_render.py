"""pygame glow renderer for the star map — the ONLY module that imports pygame.

Spike-B-locked constraints: plain SRCALPHA surfaces, NO pygame.display init
anywhere (coexists with pygame.mixer TTS), unconverted blits. Rendering is
headless; Phase C blits the finished frame into Tk via surface_to_ppm().
"""
from __future__ import annotations

from dataclasses import dataclass

import pygame
import pygame.gfxdraw as gfx

import map_overlays as mo   # pure sov_color hash for the sovereignty tint (Task 33)

# --- palette (POC v2 / Spike A approved) -----------------------------------
BG = (10, 10, 20)
SEC_HI = (0x33, 0xB5, 0xE5)
SEC_LOW = (0xFF, 0xB3, 0x47)
SEC_NULL = (0xCC, 0x22, 0x33)
LABEL_COLOR = (200, 210, 225)
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
    and dims everything else; threat halo under-glows `halo` in red."""
    bright: frozenset[int] | None = None    # None = no range tint
    halo: frozenset[int] | None = None

    def key(self) -> tuple:
        return (tuple(sorted(self.bright)) if self.bright is not None else None,
                tuple(sorted(self.halo)) if self.halo is not None else None)


# --- cached asset factories --------------------------------------------------
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

    def __init__(self) -> None:
        self._cache: dict[tuple[tuple[int, int, int], int], pygame.Surface] = {}
        self._disc_cache: dict[tuple[tuple[int, int, int], int], pygame.Surface] = {}

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
        key = (color, radius)
        got = self._disc_cache.get(key)
        if got is None:
            got = self._build_disc(color, radius)
            self._disc_cache[key] = got
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
    """Cached AA text surfaces keyed by (text, px, color). font.init lazy."""

    def __init__(self) -> None:
        self._fonts: dict[int, pygame.font.Font] = {}
        self._cache: dict[tuple[str, int, tuple[int, int, int]], pygame.Surface] = {}

    def _font(self, px: int) -> pygame.font.Font:
        f = self._fonts.get(px)
        if f is None:
            if not pygame.font.get_init():
                pygame.font.init()
            f = pygame.font.SysFont(_FONT_NAME, px)
            self._fonts[px] = f
        return f

    def label(self, text: str, px: int, color: tuple[int, int, int]) -> pygame.Surface:
        key = (text, px, color)
        got = self._cache.get(key)
        if got is None:
            got = self._font(px).render(text, True, color)
            self._cache[key] = got
        return got


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
def average_edge_length(model) -> float:
    if not model.edges:
        return 1.0
    total = 0.0
    for a, b in model.edges:
        sa, sb = model.systems[a], model.systems[b]
        total += ((sa.x - sb.x) ** 2 + (sa.y - sb.y) ** 2) ** 0.5
    return total / len(model.edges)


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
        # Sov MAX-compose scratch (Task 33 fix): a plain-RGB surface the sov blobs
        # are MAX-composed onto before a single additive blit to the frame. Lazily
        # allocated and cached by frame size (reallocated only on a size change),
        # so a steady viewport reuses one buffer -- no per-frame allocation churn.
        self._sov_scratch: pygame.Surface | None = None
        self._sov_scratch_size: tuple[int, int] | None = None
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
        self._draw_labels(surf, st, pos, cam, vw, vh, infra)
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
        scratch = self._sov_scratch
        if scratch is None or self._sov_scratch_size != size:
            scratch = self._sov_scratch = pygame.Surface(size)   # plain RGB
            self._sov_scratch_size = size
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
        hub_r = glow_r + max(3, glow_r // 3)        # was a flat +6; scales with zoom
        ring = st.core_ring and core_r >= 2         # ring around a 1px core = blob
        glow, blit, add = self.sprites.glow, surf.blit, pygame.BLEND_RGB_ADD
        for sid, (sx, sy) in pos.items():
            color, is_hub = node_static[sid]
            dimmed = bright is not None and sid not in bright
            if halo is not None and sid in halo:
                hg = glow(SEC_NULL, glow_r + 8)
                blit(hg, (sx - hg.get_width() / 2, sy - hg.get_height() / 2),
                     special_flags=add)
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

    def _draw_labels(self, surf, st, pos, cam, vw, vh, infra=None):
        if st.system_labels:
            # Priority: hubs first, then alphabetical; occupancy grid drops overlaps.
            order = sorted(pos, key=lambda sid: (sid not in HUB_IDS,
                                                 self.model.systems[sid].name))
            items = [(sid, pos[sid][0], pos[sid][1]) for sid in order]
            for sid, sx, sy in _declutter(items, 96, 24):
                lab = self.labels.label(self.model.systems[sid].name, st.label_px,
                                        LABEL_COLOR)
                surf.blit(lab, (sx + 7, sy - lab.get_height() / 2))
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
            # Fixed (+7, -9) px offset from the node dot (like the label's +7,
            # NOT scaled by zoom -- labels don't scale their offset either); the
            # chip's left edge sits at +7 and its vertical center at -9.
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
