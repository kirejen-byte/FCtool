"""pygame glow renderer for the star map — the ONLY module that imports pygame.

Spike-B-locked constraints: plain SRCALPHA surfaces, NO pygame.display init
anywhere (coexists with pygame.mixer TTS), unconverted blits. Rendering is
headless; Phase C blits the finished frame into Tk via surface_to_ppm().
"""
from __future__ import annotations

from dataclasses import dataclass

import pygame
import pygame.gfxdraw as gfx

# --- palette (POC v2 / Spike A approved) -----------------------------------
BG = (10, 10, 20)
SEC_HI = (0x33, 0xB5, 0xE5)
SEC_LOW = (0xFF, 0xB3, 0x47)
SEC_NULL = (0xCC, 0x22, 0x33)
LABEL_COLOR = (200, 210, 225)
REGION_LABEL_COLOR = (150, 165, 195)
HUB_IDS = frozenset({30000142, 30002187, 30002659, 30002053, 30002510})

_FONT_NAME = "segoeui"


def sec_color(sec: float) -> tuple[int, int, int]:
    if sec >= 0.45:
        return SEC_HI
    return SEC_LOW if sec > 0.0 else SEC_NULL


def dim(color: tuple[int, int, int], factor: float) -> tuple[int, int, int]:
    return (int(color[0] * factor), int(color[1] * factor), int(color[2] * factor))


# --- zoom bands (spec §2.4; thresholds tunable) -----------------------------
@dataclass(frozen=True)
class BandStyle:
    glow_radius: int      # sprite radius px
    core_radius: int
    edge_width: int       # widest (dim) pass; 0 = aaline only
    edge_dim: float       # brightness of the aaline pass
    system_labels: bool
    hub_labels: bool      # spec §2.4: band M labels hub systems alongside regions
    label_px: int


BAND_STYLES = {
    "U": BandStyle(glow_radius=8, core_radius=2, edge_width=0, edge_dim=0.35,
                   system_labels=False, hub_labels=False, label_px=0),
    "M": BandStyle(glow_radius=14, core_radius=3, edge_width=2, edge_dim=0.55,
                   system_labels=False, hub_labels=True, label_px=13),
    "C": BandStyle(glow_radius=22, core_radius=3, edge_width=3, edge_dim=0.55,
                   system_labels=True, hub_labels=False, label_px=13),
}


def pick_band(visible_count: int) -> str:
    if visible_count > 2500:
        return "U"
    return "M" if visible_count >= 300 else "C"


# --- cached asset factories --------------------------------------------------
class SpriteFactory:
    """Procedural radial glow sprites, cached by (color, radius). ~1 ms total."""

    def __init__(self) -> None:
        self._cache: dict[tuple[tuple[int, int, int], int], pygame.Surface] = {}

    def glow(self, color: tuple[int, int, int], radius: int) -> pygame.Surface:
        key = (color, radius)
        got = self._cache.get(key)
        if got is None:
            s = pygame.Surface((radius, radius), pygame.SRCALPHA)
            pygame.draw.circle(s, (*color, 70), (radius // 2, radius // 2),
                               max(radius // 3, 1))
            got = pygame.transform.smoothscale(s, (radius * 2, radius * 2))
            self._cache[key] = got
        return got


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
    """P6 PPM bytes for tk.PhotoImage(data=...). ~5 ms at 1280x850 (measured)."""
    w, h = surf.get_size()
    return b"P6\n%d %d\n255\n" % (w, h) + pygame.image.tobytes(surf, "RGB")


# --- frame pipeline (spec §4.2 order) ---------------------------------------
def average_edge_length(model) -> float:
    if not model.edges:
        return 1.0
    total = 0.0
    for a, b in model.edges:
        sa, sb = model.systems[a], model.systems[b]
        total += ((sa.x - sb.x) ** 2 + (sa.y - sb.y) ** 2) ** 0.5
    return total / len(model.edges)


class Renderer:
    """Turns (MapModel, Camera) into a finished glow frame. Stateless between
    frames except caches (sprites, labels, per-region nebula info)."""

    def __init__(self, model) -> None:
        self.model = model
        self.sprites = SpriteFactory()
        self.labels = LabelFactory()
        self._region_info = self._build_region_info()

    def _build_region_info(self):
        """Per-region: (anchor_x, anchor_y, world_radius, tint) for the nebula."""
        by_region: dict[int, list] = {}
        for s in self.model.systems.values():
            by_region.setdefault(s.region_id, []).append(s)
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
               mode: str = "full", band: str | None = None) -> pygame.Surface:
        surf = pygame.Surface((vw, vh))
        surf.fill(BG)

        margin = 64.0
        x0, y0, x1, y1 = cam.visible_world_rect(vw, vh, margin_px=margin)
        visible = list(self.model.systems_in_rect(x0, y0, x1, y1))
        st = BAND_STYLES[band or pick_band(len(visible))]

        self._draw_nebula(surf, cam, vw, vh)
        pos = {sid: cam.world_to_screen(self.model.systems[sid].x,
                                        self.model.systems[sid].y, vw, vh)
               for sid in visible}
        vis_set = set(visible)

        if mode == "degraded":
            self._draw_edges_degraded(surf, cam, vw, vh, pos, vis_set)
        else:
            self._draw_edges(surf, st, pos, vis_set, cam, vw, vh)
        self._draw_systems(surf, st, pos)
        if bloom and mode != "degraded":
            _bloom_pass(surf)
        self._draw_labels(surf, st, pos, cam, vw, vh)
        return surf

    # -- passes ----------------------------------------------------------------
    def _draw_nebula(self, surf, cam, vw, vh):
        for ax, ay, wr, tint in self._region_info:
            sx, sy = cam.world_to_screen(ax, ay, vw, vh)
            r_px = wr * cam.scale
            if r_px < 24 or sx < -r_px or sy < -r_px or sx > vw + r_px or sy > vh + r_px:
                continue
            bucket = min(int(r_px / 48) * 48 + 48, 480)
            sprite = self.sprites.glow(dim(tint, 0.16), bucket // 2)
            surf.blit(sprite, (sx - sprite.get_width() / 2, sy - sprite.get_height() / 2),
                      special_flags=pygame.BLEND_RGB_ADD)

    def _edge_endpoints(self, pos, vis_set):
        systems = self.model.systems
        for a, b in self.model.edges:
            if a in vis_set or b in vis_set:
                pa = pos.get(a)
                pb = pos.get(b)
                yield a, b, pa, pb, systems[a], systems[b]

    def _draw_edges(self, surf, st, pos, vis_set, cam, vw, vh):
        for a, b, pa, pb, sa, sb in self._edge_endpoints(pos, vis_set):
            if pa is None:
                pa = cam.world_to_screen(sa.x, sa.y, vw, vh)
            if pb is None:
                pb = cam.world_to_screen(sb.x, sb.y, vw, vh)
            tint = sec_color(max(sa.sec, sb.sec))
            if st.edge_width:
                pygame.draw.line(surf, dim(tint, 0.25), pa, pb, st.edge_width)
            pygame.draw.aaline(surf, dim(tint, st.edge_dim), pa, pb)

    def _draw_edges_degraded(self, surf, cam, vw, vh, pos, vis_set):
        """Fast path (spec §4.3): crisp 1px edge layer + bloom of that layer only."""
        layer = pygame.Surface(surf.get_size())
        layer.fill((0, 0, 0))
        for a, b, pa, pb, sa, sb in self._edge_endpoints(pos, vis_set):
            if pa is None:
                pa = cam.world_to_screen(sa.x, sa.y, vw, vh)
            if pb is None:
                pb = cam.world_to_screen(sb.x, sb.y, vw, vh)
            pygame.draw.line(layer, dim(sec_color(max(sa.sec, sb.sec)), 0.6), pa, pb, 1)
        _bloom_pass(layer)
        surf.blit(layer, (0, 0), special_flags=pygame.BLEND_RGB_ADD)

    def _draw_systems(self, surf, st, pos):
        systems = self.model.systems
        glow_cache = self.sprites
        for sid, (sx, sy) in pos.items():
            s = systems[sid]
            color = sec_color(s.sec)
            radius = st.glow_radius + (6 if sid in HUB_IDS else 0)
            g = glow_cache.glow(color, radius)
            surf.blit(g, (sx - g.get_width() / 2, sy - g.get_height() / 2),
                      special_flags=pygame.BLEND_RGB_ADD)
            cr = st.core_radius
            gfx.filled_circle(surf, int(sx), int(sy), cr, (255, 255, 255))
            gfx.aacircle(surf, int(sx), int(sy), cr, color)

    def _draw_labels(self, surf, st, pos, cam, vw, vh):
        if st.system_labels:
            occupied: set[tuple[int, int]] = set()
            order = sorted(pos, key=lambda sid: (sid not in HUB_IDS,
                                                 self.model.systems[sid].name))
            for sid in order:
                sx, sy = pos[sid]
                cell = (int(sx // 96), int(sy // 24))
                if cell in occupied:
                    continue
                occupied.add(cell)
                lab = self.labels.label(self.model.systems[sid].name, st.label_px,
                                        LABEL_COLOR)
                surf.blit(lab, (sx + 7, sy - lab.get_height() / 2))
        else:
            for rid, (name, ax, ay) in sorted(self.model.region_anchors.items()):
                sx, sy = cam.world_to_screen(ax, ay, vw, vh)
                if -100 <= sx <= vw + 100 and -40 <= sy <= vh + 40:
                    lab = self.labels.label(name, 15, REGION_LABEL_COLOR)
                    surf.blit(lab, (sx - lab.get_width() / 2, sy - lab.get_height() / 2))
            if st.hub_labels:
                for sid in pos:
                    if sid in HUB_IDS:
                        sx, sy = pos[sid]
                        lab = self.labels.label(self.model.systems[sid].name,
                                                st.label_px, LABEL_COLOR)
                        surf.blit(lab, (sx + 7, sy - lab.get_height() / 2))


def _bloom_pass(surf: pygame.Surface) -> None:
    w, h = surf.get_size()
    small = pygame.transform.smoothscale(surf, (max(w // 4, 1), max(h // 4, 1)))
    big = pygame.transform.smoothscale(small, (w, h))
    surf.blit(big, (0, 0), special_flags=pygame.BLEND_RGB_ADD)
