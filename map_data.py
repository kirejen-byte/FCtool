"""Pure star-map data model: joins map_layout.json (2D render coords) with
system_coords.json (names/security/region) and stargate_jumps.json (edges).

stdlib only — no tkinter, no pygame (spec §6 dependency rule). All queries the
map GUI needs: spatial grid nearest/rect lookups, BFS gate distances, anchors.
"""
from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from app_path import bundle_dir, resolve_data_file

_GRID_CELLS = 64  # per axis; ~5.4k systems -> a few per cell


def _data_path(fname: str, override: Path | None) -> Path:
    if override is not None:
        return Path(override)
    # app_dir() wins over bundle_dir() (prefer="writable"); when neither has the
    # file, fall back to the bundled path unconditionally (historic behavior —
    # the caller surfaces the missing-file error on open()).
    resolved = resolve_data_file(fname, prefer="writable")
    return Path(resolved) if resolved is not None else Path(bundle_dir()) / fname


@dataclass(frozen=True)
class MapSystem:
    id: int
    name: str
    x: float
    y: float
    sec: float
    region_id: int


class MapModel:
    def __init__(self, systems: dict[int, MapSystem], edges: list[tuple[int, int]],
                 region_anchors: dict[int, tuple[str, float, float]],
                 adjacency: dict[int, list[int]]):
        self.systems = systems
        self.edges = edges
        self.region_anchors = region_anchors
        self._adj = adjacency
        xs = [s.x for s in systems.values()] or [0.0]
        ys = [s.y for s in systems.values()] or [0.0]
        self.bounds = (min(xs), min(ys), max(xs), max(ys))
        # Uniform (square) cells sized from the LARGER extent. A degenerate
        # (flat) axis has zero extent; sizing its cells independently would
        # floor them to 1e-9, so an off-axis query point would map to a cell
        # index millions of cells away from the systems on the flat line, and
        # the reach clamp in nearest() would then miss them entirely. One span
        # for both axes keeps every cell index within [0, _GRID_CELLS].
        _span = max(self.bounds[2] - self.bounds[0],
                    self.bounds[3] - self.bounds[1], 1e-9)
        self._cell_w = self._cell_h = _span / _GRID_CELLS
        self._grid: dict[tuple[int, int], list[int]] = {}
        for s in systems.values():
            self._grid.setdefault(self._cell(s.x, s.y), []).append(s.id)

    def _cell(self, x: float, y: float) -> tuple[int, int]:
        return (int((x - self.bounds[0]) / self._cell_w),
                int((y - self.bounds[1]) / self._cell_h))

    def nearest(self, x: float, y: float, max_dist: float) -> int | None:
        cx, cy = self._cell(x, y)
        # Clamp reach to the grid extent: degenerate (flat) bounds make a cell
        # dimension ~1e-9, which would otherwise explode the loop range.
        reach = min(max(1, int(max_dist / min(self._cell_w, self._cell_h)) + 1),
                    _GRID_CELLS + 1)
        best, best_d2 = None, max_dist * max_dist
        for gx in range(cx - reach, cx + reach + 1):
            for gy in range(cy - reach, cy + reach + 1):
                for sid in self._grid.get((gx, gy), ()):
                    s = self.systems[sid]
                    d2 = (s.x - x) ** 2 + (s.y - y) ** 2
                    if d2 <= best_d2:
                        best, best_d2 = sid, d2
        return best

    def systems_in_rect(self, x0: float, y0: float, x1: float, y1: float):
        for s in self.systems.values():
            if x0 <= s.x <= x1 and y0 <= s.y <= y1:
                yield s.id

    def gate_distances(self, origin: int) -> dict[int, int]:
        if origin not in self.systems:
            return {}
        dist = {origin: 0}
        q = deque([origin])
        while q:
            cur = q.popleft()
            for nb in self._adj.get(cur, ()):
                if nb not in dist and nb in self.systems:
                    dist[nb] = dist[cur] + 1
                    q.append(nb)
        return dist


def load_map_model(layout_path: Path | None = None, coords_path: Path | None = None,
                   gates_path: Path | None = None) -> MapModel:
    layout = json.loads(_data_path("map_layout.json", layout_path).read_text(encoding="utf-8"))
    coords = json.loads(_data_path("system_coords.json", coords_path).read_text(encoding="utf-8"))
    gates = json.loads(_data_path("stargate_jumps.json", gates_path).read_text(encoding="utf-8"))

    systems: dict[int, MapSystem] = {}
    for sid_s, pos in layout["systems"].items():
        c = coords.get(sid_s)
        if c is None:
            continue  # layout entry with no 3D record: cannot label/color -> drop
        sid = int(sid_s)
        systems[sid] = MapSystem(sid, c.get("name", sid_s), float(pos["x"]), float(pos["y"]),
                                 float(c.get("security", 0.0)), int(c.get("region_id", 0)))

    adjacency = {int(a): [int(b) for b in nbrs] for a, nbrs in gates.items()}
    edges = sorted({(min(int(a), b), max(int(a), b))
                    for a, nbrs in gates.items() for b in nbrs
                    if int(a) in systems and b in systems})
    anchors = {int(rid): (r["name"], float(r["lx"]), float(r["ly"]))
               for rid, r in layout["regions"].items()}
    return MapModel(systems, edges, anchors, adjacency)
