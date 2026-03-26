"""
Jump Range Calculator
Uses ESI to calculate stargate routes and determine if systems are within
capital jump drive range using system coordinates.
"""

import csv
import io
import json
import math
import os
import requests
import threading
from collections import deque
from rate_limiter import rate_limit
from app_path import app_dir

ESI_BASE = "https://esi.evetech.net/latest"
HEADERS = {"User-Agent": "FCTool/1.0 (EVE FC Assistant)"}

# 1 light year in meters (EVE uses this constant)
LY_IN_METERS = 9.4605e15

# ── Persistent disk caches ─────────────────────────────────────────────────
# System data essentially never changes, so cache everything to disk.
_CACHE_DIR = app_dir()
_ROUTE_CACHE_FILE = os.path.join(_CACHE_DIR, "routes_cache.json")
_SYSTEM_CACHE_FILE = os.path.join(_CACHE_DIR, "esi_cache.json")

_route_disk_cache: dict[str, list[int]] = {}
_system_disk_cache: dict[str, dict] = {}  # system_id -> {position, name, ...}
_system_name_cache: dict[str, int] = {}   # lowered name -> system_id
_cache_lock = threading.Lock()
_cache_dirty = False


def _load_caches():
    global _route_disk_cache, _system_disk_cache, _system_name_cache
    try:
        if os.path.exists(_ROUTE_CACHE_FILE):
            with open(_ROUTE_CACHE_FILE, "r") as f:
                _route_disk_cache = json.load(f)
            print(f"[Cache] Loaded {len(_route_disk_cache)} cached routes")
    except Exception:
        _route_disk_cache = {}
    try:
        if os.path.exists(_SYSTEM_CACHE_FILE):
            with open(_SYSTEM_CACHE_FILE, "r") as f:
                data = json.load(f)
            _system_disk_cache = data.get("systems", {})
            _system_name_cache = data.get("names", {})
            print(f"[Cache] Loaded {len(_system_disk_cache)} systems, {len(_system_name_cache)} name lookups")
    except Exception:
        _system_disk_cache = {}
        _system_name_cache = {}


def save_route_cache():
    """Save all caches to disk. Call periodically or on shutdown."""
    global _cache_dirty
    if not _cache_dirty:
        return
    try:
        with _cache_lock:
            with open(_ROUTE_CACHE_FILE, "w") as f:
                json.dump(_route_disk_cache, f)
            with open(_SYSTEM_CACHE_FILE, "w") as f:
                json.dump({"systems": _system_disk_cache, "names": _system_name_cache}, f)
            _cache_dirty = False
    except Exception:
        pass


# Load on import
_load_caches()


# ── Local stargate graph (for Zarzakh-free routing) ─────────────────────────
# ESI's route API routes through Zarzakh, which the in-game autopilot does not.
# We download the stargate connection map from the EVE SDE and do BFS locally
# when ESI returns a route containing Zarzakh.
ZARZAKH_ID = 30100000
_JUMPS_CACHE_FILE = os.path.join(_CACHE_DIR, "stargate_jumps.json")
_stargate_graph: dict[int, set[int]] = {}  # system_id -> set of connected system_ids
_stargate_graph_loaded = False
_stargate_graph_lock = threading.Lock()


def _load_stargate_graph():
    """Load or download the stargate connection graph."""
    global _stargate_graph, _stargate_graph_loaded
    if _stargate_graph_loaded:
        return

    with _stargate_graph_lock:
        if _stargate_graph_loaded:
            return

        # Try loading from disk cache first
        if os.path.exists(_JUMPS_CACHE_FILE):
            try:
                with open(_JUMPS_CACHE_FILE, "r") as f:
                    data = json.load(f)
                for sys_id, neighbors in data.items():
                    _stargate_graph[int(sys_id)] = set(neighbors)
                _stargate_graph_loaded = True
                print(f"[Graph] Loaded stargate graph: {len(_stargate_graph)} systems")
                return
            except Exception:
                pass

        # Download from fuzzwork SDE
        try:
            print("[Graph] Downloading stargate connection map...")
            resp = requests.get(
                "https://www.fuzzwork.co.uk/dump/latest/mapSolarSystemJumps.csv",
                timeout=30
            )
            if resp.ok:
                reader = csv.DictReader(io.StringIO(resp.text))
                for row in reader:
                    from_sys = int(row["fromSolarSystemID"])
                    to_sys = int(row["toSolarSystemID"])
                    _stargate_graph.setdefault(from_sys, set()).add(to_sys)
                    _stargate_graph.setdefault(to_sys, set()).add(from_sys)

                # Save to disk
                serializable = {str(k): list(v) for k, v in _stargate_graph.items()}
                with open(_JUMPS_CACHE_FILE, "w") as f:
                    json.dump(serializable, f)
                _stargate_graph_loaded = True
                print(f"[Graph] Downloaded stargate graph: {len(_stargate_graph)} systems")
        except Exception as e:
            print(f"[Graph] Failed to download stargate graph: {e}")


def _bfs_route(origin_id: int, dest_id: int,
               exclude: set[int] | None = None,
               extra_connections: list[str] | None = None) -> list[int] | None:
    """
    BFS shortest path through the stargate graph, with optional exclusions
    and extra connections (Ansiblex gates).
    """
    _load_stargate_graph()
    if not _stargate_graph:
        return None

    if exclude is None:
        exclude = set()

    # Build ansiblex adjacency
    ansiblex_adj: dict[int, set[int]] = {}
    if extra_connections:
        for conn in extra_connections:
            parts = conn.split("|")
            if len(parts) == 2:
                a, b = int(parts[0]), int(parts[1])
                ansiblex_adj.setdefault(a, set()).add(b)
                ansiblex_adj.setdefault(b, set()).add(a)

    # BFS with parent tracking (memory efficient)
    if origin_id == dest_id:
        return [origin_id]

    parent: dict[int, int] = {origin_id: -1}
    queue = deque([origin_id])

    while queue:
        current = queue.popleft()

        # Get neighbors from stargates + ansiblex
        neighbors = _stargate_graph.get(current, set())
        if current in ansiblex_adj:
            neighbors = neighbors | ansiblex_adj[current]

        for neighbor in neighbors:
            if neighbor in parent or neighbor in exclude:
                continue
            parent[neighbor] = current
            if neighbor == dest_id:
                # Reconstruct path
                path = []
                node = dest_id
                while node != -1:
                    path.append(node)
                    node = parent[node]
                path.reverse()
                return path
            queue.append(neighbor)

    return None


def get_system_info(system_id: int) -> dict | None:
    """Get system info including position from ESI. Persistently cached to disk."""
    global _cache_dirty
    key = str(system_id)
    with _cache_lock:
        if key in _system_disk_cache:
            return _system_disk_cache[key]
    try:
        rate_limit("esi")
        resp = requests.get(
            f"{ESI_BASE}/universe/systems/{system_id}/",
            headers=HEADERS, timeout=10
        )
        if resp.ok:
            data = resp.json()
            with _cache_lock:
                _system_disk_cache[key] = data
                _cache_dirty = True
            return data
    except Exception:
        pass
    return None


def search_system(name: str) -> int | None:
    """Search for a system by name, return its ID. Persistently cached to disk."""
    global _cache_dirty
    lower = name.lower()
    with _cache_lock:
        if lower in _system_name_cache:
            return _system_name_cache[lower]
    try:
        rate_limit("esi")
        resp = requests.post(
            f"{ESI_BASE}/universe/ids/",
            json=[name],
            headers={**HEADERS, "Content-Type": "application/json"},
            timeout=10,
        )
        if resp.ok:
            systems = resp.json().get("systems", [])
            result_id = None
            # Find exact match (case-insensitive)
            for s in systems:
                if s["name"].lower() == lower:
                    result_id = s["id"]
                    break
            if result_id is None and systems:
                result_id = systems[0]["id"]
            if result_id is not None:
                with _cache_lock:
                    _system_name_cache[lower] = result_id
                    _cache_dirty = True
                return result_id
    except Exception:
        pass
    return None


def get_stargate_route(origin_id: int, destination_id: int,
                       preference: str = "shortest",
                       connections: list[str] | None = None) -> list[int] | None:
    """
    Get a stargate route between two systems using local BFS pathfinding.
    Excludes Zarzakh (ESI treats its gates as normal stargates but in-game
    autopilot does not route through Zarzakh).

    Uses persistent disk cache since stargate topology doesn't change.

    connections: optional list of Ansiblex pairs as "id1|id2" strings.
                 These are added as extra edges in the BFS graph.
    """
    global _cache_dirty
    # Include connections in cache key so Ansiblex routes cache separately
    conn_suffix = ""
    if connections:
        conn_suffix = ":" + ",".join(sorted(connections))
    cache_key = f"{origin_id}:{destination_id}{conn_suffix}"

    # Check disk cache first
    with _cache_lock:
        if cache_key in _route_disk_cache:
            val = _route_disk_cache[cache_key]
            return val if val else None

    # Local BFS (primary method — accurate, no Zarzakh)
    route = _bfs_route(origin_id, destination_id,
                       exclude={ZARZAKH_ID},
                       extra_connections=connections)

    if route:
        with _cache_lock:
            _route_disk_cache[cache_key] = route
            _cache_dirty = True
        return route

    # Cache negative result
    with _cache_lock:
        _route_disk_cache[cache_key] = []
        _cache_dirty = True
    return None


def calculate_ly_distance(system_a_id: int, system_b_id: int) -> float | None:
    """Calculate the light-year distance between two systems."""
    info_a = get_system_info(system_a_id)
    info_b = get_system_info(system_b_id)
    if not info_a or not info_b:
        return None

    pos_a = info_a.get("position", {})
    pos_b = info_b.get("position", {})
    if not pos_a or not pos_b:
        return None

    dx = pos_a["x"] - pos_b["x"]
    dy = pos_a["y"] - pos_b["y"]
    dz = pos_a["z"] - pos_b["z"]
    distance_m = math.sqrt(dx*dx + dy*dy + dz*dz)
    return distance_m / LY_IN_METERS


def is_in_jump_range(origin_id: int, destination_id: int, range_ly: float) -> tuple[bool, float | None]:
    """
    Check if destination is within jump drive range of origin.
    Returns (in_range, distance_ly).
    """
    dist = calculate_ly_distance(origin_id, destination_id)
    if dist is None:
        return False, None
    return dist <= range_ly, round(dist, 2)


class JumpRangeChecker:
    """
    Checks jump range between systems for capital ships.
    Also provides stargate route calculation.
    """

    # Base jump ranges in LY (at JDC 5)
    SHIP_RANGES = {
        "Dreadnought": 7.0,
        "Carrier": 7.0,
        "Force Auxiliary": 7.0,
        "Supercarrier": 6.0,
        "Titan": 6.0,
        "Black Ops": 8.0,
        "Jump Freighter": 10.0,
        "Rorqual": 5.0,
    }

    def __init__(self, ship_type: str = "Dreadnought", jdc_level: int = 5,
                 custom_ranges: dict | None = None):
        self.ship_type = ship_type
        self.jdc_level = jdc_level
        if custom_ranges:
            self.SHIP_RANGES.update(custom_ranges)

    @property
    def jump_range(self) -> float:
        base = self.SHIP_RANGES.get(self.ship_type, 7.0)
        # JDC adds 25% per level to base range
        return base * (1 + 0.25 * self.jdc_level) / (1 + 0.25 * 5)

    def check_range(self, origin: str, destination: str,
                    connections: list[str] | None = None) -> dict:
        """
        Check if destination is in jump range of origin.
        Accepts system names.
        Returns dict with results.
        """
        origin_id = search_system(origin)
        dest_id = search_system(destination)

        if not origin_id:
            return {"error": f"System not found: {origin}"}
        if not dest_id:
            return {"error": f"System not found: {destination}"}

        in_range, distance = is_in_jump_range(origin_id, dest_id, self.jump_range)

        result = {
            "origin": origin,
            "destination": destination,
            "origin_id": origin_id,
            "destination_id": dest_id,
            "distance_ly": distance,
            "jump_range_ly": round(self.jump_range, 2),
            "ship_type": self.ship_type,
            "in_range": in_range,
        }

        # Also get stargate route for context
        route = get_stargate_route(origin_id, dest_id, connections=connections)
        if route:
            result["gate_jumps"] = len(route) - 1
        else:
            result["gate_jumps"] = None

        return result

    def find_systems_in_range(self, origin: str, system_list: list[str]) -> list[dict]:
        """Check which systems from a list are in jump range."""
        results = []
        for system_name in system_list:
            result = self.check_range(origin, system_name)
            results.append(result)
        return [r for r in results if r.get("in_range")]
