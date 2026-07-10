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
