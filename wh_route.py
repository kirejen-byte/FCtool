"""
Wormhole Route Finder
Uses EVE Scout API to find shortcuts through Thera/Turnur wormhole connections.
Compares direct stargate route vs routes using wormhole hubs.
"""

import requests
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from esi_constants import ESI_HEADERS_JSON as HEADERS
from jump_range import search_system, get_stargate_route, save_route_cache
from rate_limiter import rate_limit

EVESCOUT_API = "https://api.eve-scout.com/v2/public/signatures"


@dataclass
class WHConnection:
    """A single wormhole connection from EVE Scout."""
    hub_name: str            # "Thera" or "Turnur"
    hub_system_id: int
    hub_signature: str       # e.g. "EBJ-826"
    dest_system_id: int
    dest_system_name: str
    dest_region_name: str
    dest_security_class: str  # "hs", "ls", "ns", "c1", etc.
    dest_signature: str       # e.g. "BCX-295"
    max_ship_size: str        # "medium", "large", "xlarge", "capital"
    expires_at: str
    remaining_hours: int
    wh_type: str


@dataclass
class WHRoute:
    """A route that uses a wormhole shortcut."""
    origin: str
    destination: str
    gate_jumps_direct: int | None       # Direct stargate route
    total_jumps_via_wh: int | None      # Total jumps using WH shortcut
    jumps_saved: int                     # How many jumps shorter
    legs: list[dict] = field(default_factory=list)  # Route breakdown
    hub_name: str = ""                   # Which hub used
    entry_connection: WHConnection | None = None
    exit_connection: WHConnection | None = None


def fetch_connections() -> list[WHConnection]:
    """Fetch current Thera/Turnur connections from EVE Scout API."""
    try:
        rate_limit("evescout")
        resp = requests.get(EVESCOUT_API, headers=HEADERS, timeout=15)
        if not resp.ok:
            return []

        connections = []
        for sig in resp.json():
            if not sig.get("completed"):
                continue  # Skip incomplete scans

            connections.append(WHConnection(
                hub_name=sig.get("out_system_name", ""),
                hub_system_id=sig.get("out_system_id", 0),
                hub_signature=sig.get("out_signature", ""),
                dest_system_id=sig.get("in_system_id", 0),
                dest_system_name=sig.get("in_system_name", ""),
                dest_region_name=sig.get("in_region_name", ""),
                dest_security_class=sig.get("in_system_class", ""),
                dest_signature=sig.get("in_signature", ""),
                max_ship_size=sig.get("max_ship_size", ""),
                expires_at=sig.get("expires_at", ""),
                remaining_hours=sig.get("remaining_hours", 0),
                wh_type=sig.get("wh_type", ""),
            ))
        return connections
    except Exception as e:
        print(f"[WHRoute] Error fetching EVE Scout data: {e}")
        return []


def find_wh_route(origin_name: str, destination_name: str,
                  ship_size: str = "any",
                  connections: list[str] | None = None) -> WHRoute | None:
    """
    Find the best wormhole shortcut between two systems.

    Args:
        connections: Ansiblex gate pairs as "id1|id2" strings for ESI routing.

    Optimized:
    - Pre-computes all entry/exit leg distances in O(2N) instead of O(N²)
    - Uses concurrent requests (ThreadPoolExecutor) for parallel ESI calls
    - All routes are persistently cached to disk (stargate routes don't change)
    - Second search for same/nearby systems is near-instant
    """
    origin_id = search_system(origin_name)
    dest_id = search_system(destination_name)

    if not origin_id or not dest_id:
        return None

    # Preserve Ansiblex connections separately from WH connections
    ansiblex_conns = connections  # "id1|id2" strings or None

    # Get direct route for comparison (including Ansiblex if configured)
    direct_route = get_stargate_route(origin_id, dest_id, connections=ansiblex_conns)
    direct_jumps = (len(direct_route) - 1) if direct_route else None

    # Fetch current wormhole connections
    wh_connections = fetch_connections()
    if not wh_connections:
        return WHRoute(
            origin=origin_name, destination=destination_name,
            gate_jumps_direct=direct_jumps,
            total_jumps_via_wh=None, jumps_saved=0,
        )

    # Filter by ship size if specified
    size_order = {"medium": 0, "large": 1, "xlarge": 2, "capital": 3}
    if ship_size != "any" and ship_size in size_order:
        min_size = size_order[ship_size]
        wh_connections = [c for c in wh_connections
                          if size_order.get(c.max_ship_size, -1) >= min_size]

    # Get unique destination systems from WH connections
    unique_systems = list({c.dest_system_id for c in wh_connections})

    # Fetch ALL routes concurrently — both legs in parallel
    leg1_jumps: dict[int, int] = {}  # dest_system_id -> gate jumps from origin
    leg3_jumps: dict[int, int] = {}  # dest_system_id -> gate jumps to destination

    def fetch_leg1(sys_id):
        route = get_stargate_route(origin_id, sys_id, connections=ansiblex_conns)
        return sys_id, (len(route) - 1) if route else None

    def fetch_leg3(sys_id):
        route = get_stargate_route(sys_id, dest_id, connections=ansiblex_conns)
        return sys_id, (len(route) - 1) if route else None

    # Run all route lookups concurrently (rate limiter controls actual throughput)
    with ThreadPoolExecutor(max_workers=10) as executor:
        all_futures = []
        for sys_id in unique_systems:
            all_futures.append(("leg1", executor.submit(fetch_leg1, sys_id)))
            all_futures.append(("leg3", executor.submit(fetch_leg3, sys_id)))

        for leg_type, future in all_futures:
            try:
                sys_id, jumps = future.result(timeout=30)
                if jumps is not None:
                    if leg_type == "leg1":
                        leg1_jumps[sys_id] = jumps
                    else:
                        leg3_jumps[sys_id] = jumps
            except Exception:
                pass

    # Save any newly cached routes to disk
    save_route_cache()

    # Group WH connections by hub
    hubs: dict[str, list[WHConnection]] = {}
    for conn in wh_connections:
        hubs.setdefault(conn.hub_name, []).append(conn)

    best_route: WHRoute | None = None
    best_total = float("inf")

    for hub_name, hub_conns in hubs.items():
        for entry in hub_conns:
            if entry.dest_system_id not in leg1_jumps:
                continue
            l1 = leg1_jumps[entry.dest_system_id]

            for exit_conn in hub_conns:
                if exit_conn.dest_system_id == entry.dest_system_id:
                    continue
                if exit_conn.dest_system_id not in leg3_jumps:
                    continue

                l3 = leg3_jumps[exit_conn.dest_system_id]
                total = l1 + 1 + 1 + l3  # gates + WH in + WH out + gates

                if total < best_total:
                    best_total = total
                    best_route = WHRoute(
                        origin=origin_name,
                        destination=destination_name,
                        gate_jumps_direct=direct_jumps,
                        total_jumps_via_wh=total,
                        jumps_saved=(direct_jumps - total) if direct_jumps else 0,
                        hub_name=hub_name,
                        entry_connection=entry,
                        exit_connection=exit_conn,
                        legs=[
                            {
                                "type": "gate",
                                "from": origin_name,
                                "to": entry.dest_system_name,
                                "jumps": l1,
                                "description": f"Gate {origin_name} -> {entry.dest_system_name}",
                            },
                            {
                                "type": "wormhole",
                                "from": entry.dest_system_name,
                                "to": hub_name,
                                "jumps": 1,
                                "signature": entry.dest_signature,
                                "wh_type": entry.wh_type,
                                "max_ship_size": entry.max_ship_size,
                                "remaining_hours": entry.remaining_hours,
                                "description": f"WH {entry.dest_signature} -> {hub_name} ({entry.wh_type}, {entry.max_ship_size})",
                            },
                            {
                                "type": "wormhole",
                                "from": hub_name,
                                "to": exit_conn.dest_system_name,
                                "jumps": 1,
                                "signature": exit_conn.hub_signature,
                                "wh_type": exit_conn.wh_type,
                                "max_ship_size": exit_conn.max_ship_size,
                                "remaining_hours": exit_conn.remaining_hours,
                                "description": f"WH {exit_conn.hub_signature} -> {exit_conn.dest_system_name} ({exit_conn.wh_type}, {exit_conn.max_ship_size})",
                            },
                            {
                                "type": "gate",
                                "from": exit_conn.dest_system_name,
                                "to": destination_name,
                                "jumps": l3,
                                "description": f"Gate {exit_conn.dest_system_name} -> {destination_name}",
                            },
                        ],
                    )

    if best_route is None:
        return WHRoute(
            origin=origin_name, destination=destination_name,
            gate_jumps_direct=direct_jumps,
            total_jumps_via_wh=None, jumps_saved=0,
        )

    return best_route
