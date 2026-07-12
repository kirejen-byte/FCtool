"""Pure camera math for the star map: world<->screen transforms, cursor-anchored
zoom with clamping, fit-to-bounds, visible-rect culling helper, and gesture
settle timing. stdlib only — no tkinter, no pygame (spec §6 dependency rule).

World space = map_layout.json coordinates (magnitude ~1e17); scale is px per
world unit (magnitude ~1e-15 at universe framing). Screen origin is top-left.
"""
from __future__ import annotations


def scale_limits(bounds: tuple[float, float, float, float], avg_edge_len: float,
                 vw: int, vh: int, pad_frac: float = 0.05) -> tuple[float, float]:
    """(min_scale, max_scale): 0.9x the fit-universe scale up to 64px per typical
    (median) edge. The 48->64 bump + median edge length (vs mean, which long
    inter-region edges inflate ~2x) let dense regions reach band C (Phase B
    checkpoint fix)."""
    fit = _fit_scale(bounds, vw, vh, pad_frac=0.0)
    max_scale = 64.0 / avg_edge_len if avg_edge_len > 0 else fit * 1000.0
    return fit * 0.9, max(max_scale, fit)  # never let max < fit


def _fit_scale(bounds, vw, vh, pad_frac):
    x0, y0, x1, y1 = bounds
    span_x = max(x1 - x0, 1e-30)
    span_y = max(y1 - y0, 1e-30)
    return min(vw / span_x, vh / span_y) / (1.0 + pad_frac)


class Camera:
    def __init__(self) -> None:
        self.cx = 0.0
        self.cy = 0.0
        self.scale = 1.0
        self.min_scale = 1e-30
        self.max_scale = 1e30

    def set_scale_limits(self, lo: float, hi: float) -> None:
        self.min_scale, self.max_scale = lo, hi
        self.scale = min(max(self.scale, lo), hi)

    def fit_bounds(self, bounds: tuple[float, float, float, float],
                   vw: int, vh: int, pad_frac: float = 0.05) -> None:
        x0, y0, x1, y1 = bounds
        self.cx = (x0 + x1) / 2.0
        self.cy = (y0 + y1) / 2.0
        self.scale = _fit_scale(bounds, vw, vh, pad_frac)

    def world_to_screen(self, wx: float, wy: float, vw: int, vh: int) -> tuple[float, float]:
        return ((wx - self.cx) * self.scale + vw / 2.0,
                (wy - self.cy) * self.scale + vh / 2.0)

    def screen_to_world(self, sx: float, sy: float, vw: int, vh: int) -> tuple[float, float]:
        return ((sx - vw / 2.0) / self.scale + self.cx,
                (sy - vh / 2.0) / self.scale + self.cy)

    def zoom_at(self, factor: float, sx: float, sy: float, vw: int, vh: int) -> None:
        """Scale by factor (clamped), keeping the world point under (sx, sy) fixed."""
        wx, wy = self.screen_to_world(sx, sy, vw, vh)
        self.scale = min(max(self.scale * factor, self.min_scale), self.max_scale)
        # Re-solve center so (wx, wy) projects back to (sx, sy):
        self.cx = wx - (sx - vw / 2.0) / self.scale
        self.cy = wy - (sy - vh / 2.0) / self.scale

    def pan_pixels(self, dx_px: float, dy_px: float) -> None:
        """Move the map dx/dy pixels (drag direction); camera center moves opposite."""
        self.cx -= dx_px / self.scale
        self.cy -= dy_px / self.scale

    def visible_world_rect(self, vw: int, vh: int, margin_px: float = 0.0):
        x0, y0 = self.screen_to_world(-margin_px, -margin_px, vw, vh)
        x1, y1 = self.screen_to_world(vw + margin_px, vh + margin_px, vw, vh)
        return x0, y0, x1, y1

    def to_dict(self) -> dict:
        return {"cx": self.cx, "cy": self.cy, "scale": self.scale}

    @classmethod
    def from_dict(cls, d: dict) -> "Camera":
        cam = cls()
        cam.cx = float(d.get("cx", 0.0))
        cam.cy = float(d.get("cy", 0.0))
        cam.scale = float(d.get("scale", 1.0))
        return cam


class GestureTracker:
    """Settle detection: a crisp re-render is due settle_ms after the last input.

    Time is injected (now_ms) so the logic is testable and clock-agnostic;
    Phase C feeds it a monotonic milliseconds clock.
    """

    def __init__(self, settle_ms: float = 120.0) -> None:
        self.settle_ms = settle_ms
        self._last_touch_ms: float | None = None

    def touch(self, now_ms: float) -> None:
        self._last_touch_ms = now_ms

    def is_settled(self, now_ms: float) -> bool:
        if self._last_touch_ms is None:
            return True
        return (now_ms - self._last_touch_ms) > self.settle_ms


class ZoomAnimator:
    """Exponential ease toward a target scale. tick() returns the factor to
    apply this frame (via Camera.zoom_at at the stored anchor) or None when idle.
    Convergence: ~95% of the remaining distance covered in ~120 ms."""

    RATE = 0.022  # per-ms exponential rate: 1-exp(-0.022*120) ~ 0.93

    def __init__(self) -> None:
        self.active = False
        self._target = 1.0
        self._anchor = (0.0, 0.0)
        self._last_ms = 0.0

    def start(self, current_scale: float, factor: float, sx: float, sy: float,
              now_ms: float, min_scale: float, max_scale: float) -> None:
        base = self._target if self.active else current_scale
        self._target = min(max(base * factor, min_scale), max_scale)
        self._anchor = (sx, sy)
        self._last_ms = now_ms
        self.active = True

    def tick(self, current_scale: float, now_ms: float) -> float | None:
        if not self.active:
            return None
        dt = max(now_ms - self._last_ms, 0.0)
        self._last_ms = now_ms
        import math
        alpha = 1.0 - math.exp(-self.RATE * dt)
        new_scale = current_scale + (self._target - current_scale) * alpha
        if abs(new_scale / self._target - 1.0) < 0.004:
            new_scale = self._target
            self.active = False
        return new_scale / current_scale        # factor for Camera.zoom_at

    @property
    def anchor(self) -> tuple[float, float]:
        return self._anchor
