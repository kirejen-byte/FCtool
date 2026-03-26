"""
EVE System Name Cache
Downloads and caches all K-space system names from ESI for autocomplete.
Also builds a system->region mapping for display.
Excludes wormhole systems (J-space) unless they have known EVE Scout connections.
"""

import json
import os
import time
import requests
from concurrent.futures import ThreadPoolExecutor
from rate_limiter import rate_limit
from app_path import app_dir

ESI_BASE = "https://esi.evetech.net/latest"
HEADERS = {"User-Agent": "FCTool/1.0 (EVE FC Assistant)"}
CACHE_FILE = os.path.join(app_dir(), "systems_cache.json")
REGION_CACHE_FILE = os.path.join(app_dir(), "regions_cache.json")
CACHE_MAX_AGE = 7 * 24 * 3600  # Refresh weekly (system list rarely changes)


def _is_kspace_system(system_id: int) -> bool:
    """K-space systems have IDs 30000000-30999999. J-space is 31000000+."""
    return 30000000 <= system_id <= 30999999


def download_system_names() -> dict[str, int]:
    """
    Download all K-space system names from ESI.
    Returns {name: system_id} dict.
    """
    print("[SystemCache] Downloading system list from ESI...")

    # Step 1: Get all system IDs
    rate_limit("esi")
    resp = requests.get(f"{ESI_BASE}/universe/systems/", headers=HEADERS, timeout=30)
    if not resp.ok:
        print(f"[SystemCache] Failed to fetch system IDs: {resp.status_code}")
        return {}

    all_ids = resp.json()
    # Filter to K-space only
    kspace_ids = [sid for sid in all_ids if _is_kspace_system(sid)]
    print(f"[SystemCache] {len(kspace_ids)} K-space systems found, resolving names...")

    # Step 2: Batch resolve names (ESI accepts up to 1000 per request)
    systems = {}
    batch_size = 1000
    for i in range(0, len(kspace_ids), batch_size):
        batch = kspace_ids[i:i + batch_size]
        rate_limit("esi")
        resp = requests.post(
            f"{ESI_BASE}/universe/names/",
            json=batch,
            headers={**HEADERS, "Content-Type": "application/json"},
            timeout=30,
        )
        if resp.ok:
            for entry in resp.json():
                if entry.get("category") == "solar_system":
                    systems[entry["name"]] = entry["id"]
        else:
            print(f"[SystemCache] Batch {i//batch_size + 1} failed: {resp.status_code}")

        # Progress
        done = min(i + batch_size, len(kspace_ids))
        print(f"[SystemCache] Resolved {done}/{len(kspace_ids)} systems...")

    print(f"[SystemCache] Done. {len(systems)} systems cached.")
    return systems


def save_cache(systems: dict[str, int]):
    """Save system cache to disk."""
    data = {
        "timestamp": time.time(),
        "systems": systems,
    }
    with open(CACHE_FILE, "w") as f:
        json.dump(data, f)


def load_cache() -> dict[str, int] | None:
    """Load system cache from disk if fresh enough."""
    if not os.path.exists(CACHE_FILE):
        return None
    try:
        with open(CACHE_FILE, "r") as f:
            data = json.load(f)
        age = time.time() - data.get("timestamp", 0)
        if age > CACHE_MAX_AGE:
            print("[SystemCache] Cache expired, will refresh.")
            return None
        return data.get("systems", {})
    except Exception:
        return None


def get_system_names() -> dict[str, int]:
    """
    Get all K-space system names. Uses cache if available, downloads if not.
    Returns {name: system_id} dict.
    """
    cached = load_cache()
    if cached:
        return cached

    systems = download_system_names()
    if systems:
        save_cache(systems)
    return systems


def get_sorted_names() -> list[str]:
    """Get a sorted list of all K-space system names for autocomplete."""
    systems = get_system_names()
    return sorted(systems.keys())


# ── Region Mapping ─────────────────────────────────────────────────────────
# Maps system_id -> region_name, built top-down: regions -> constellations -> systems

def download_region_map() -> dict[str, str]:
    """
    Build a system_id -> region_name mapping via top-down ESI traversal.
    Returns {str(system_id): region_name} dict.
    ~67 region calls + ~1100 constellation calls, run concurrently.
    """
    print("[RegionCache] Building region map from ESI...")

    # Step 1: Get all region IDs
    rate_limit("esi")
    resp = requests.get(f"{ESI_BASE}/universe/regions/", headers=HEADERS, timeout=30)
    if not resp.ok:
        print(f"[RegionCache] Failed to fetch region IDs: {resp.status_code}")
        return {}
    region_ids = resp.json()

    # Step 2: Fetch all regions (name + constellation list)
    region_names: dict[int, str] = {}
    constellation_to_region: dict[int, int] = {}

    def fetch_region(rid):
        rate_limit("esi")
        r = requests.get(f"{ESI_BASE}/universe/regions/{rid}/", headers=HEADERS, timeout=10)
        if r.ok:
            data = r.json()
            return rid, data.get("name", ""), data.get("constellations", [])
        return rid, "", []

    print(f"[RegionCache] Fetching {len(region_ids)} regions...")
    with ThreadPoolExecutor(max_workers=10) as executor:
        for result in executor.map(fetch_region, region_ids):
            rid, name, consts = result
            if name:
                region_names[rid] = name
                for cid in consts:
                    constellation_to_region[cid] = rid

    # Step 3: Fetch all constellations to get system lists
    system_to_region: dict[str, str] = {}
    constellation_ids = list(constellation_to_region.keys())

    def fetch_constellation(cid):
        rate_limit("esi")
        r = requests.get(f"{ESI_BASE}/universe/constellations/{cid}/", headers=HEADERS, timeout=10)
        if r.ok:
            return cid, r.json().get("systems", [])
        return cid, []

    print(f"[RegionCache] Fetching {len(constellation_ids)} constellations...")
    with ThreadPoolExecutor(max_workers=10) as executor:
        for result in executor.map(fetch_constellation, constellation_ids):
            cid, system_ids = result
            rid = constellation_to_region.get(cid)
            if rid and rid in region_names:
                rname = region_names[rid]
                for sid in system_ids:
                    system_to_region[str(sid)] = rname

    print(f"[RegionCache] Done. {len(system_to_region)} systems mapped to regions.")
    return system_to_region


def save_region_cache(region_map: dict[str, str]):
    """Save region mapping to disk."""
    data = {"timestamp": time.time(), "regions": region_map}
    with open(REGION_CACHE_FILE, "w") as f:
        json.dump(data, f)


def load_region_cache() -> dict[str, str] | None:
    """Load region cache from disk."""
    if not os.path.exists(REGION_CACHE_FILE):
        return None
    try:
        with open(REGION_CACHE_FILE, "r") as f:
            data = json.load(f)
        # Region assignments never change, so no expiry needed
        return data.get("regions", {})
    except Exception:
        return None


def get_region_map() -> dict[str, str]:
    """
    Get system_id -> region_name mapping. Uses cache if available.
    Returns {str(system_id): region_name}.
    """
    cached = load_region_cache()
    if cached:
        return cached

    region_map = download_region_map()
    if region_map:
        save_region_cache(region_map)
    return region_map
