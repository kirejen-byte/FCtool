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
from app_io import atomic_write_json
from app_log import get_logger
from esi_constants import ESI_BASE, ESI_HEADERS as HEADERS

log = get_logger(__name__)

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
    atomic_write_json(CACHE_FILE, data, indent=None)


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
        log.exception("Failed to load system cache %s; discarding", CACHE_FILE)
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

def _download_region_data() -> tuple[dict[str, str], dict[str, int]]:
    """
    Build region mappings via top-down ESI traversal.

    Returns a 2-tuple:
      - system_to_region: {str(system_id): region_name}
      - region_name_to_id: {region_name: region_id}

    ~67 region calls + ~1100 constellation calls, run concurrently.
    On total failure (can't fetch region IDs) returns ({}, {}).
    """
    print("[RegionCache] Building region map from ESI...")

    # Step 1: Get all region IDs
    rate_limit("esi")
    resp = requests.get(f"{ESI_BASE}/universe/regions/", headers=HEADERS, timeout=30)
    if not resp.ok:
        print(f"[RegionCache] Failed to fetch region IDs: {resp.status_code}")
        return {}, {}
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

    # Build region NAME -> ID map (instant/local region resolution later).
    region_name_to_id: dict[str, int] = {
        name: rid for rid, name in region_names.items()
    }

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

    print(
        f"[RegionCache] Done. {len(system_to_region)} systems mapped to "
        f"{len(region_name_to_id)} regions."
    )
    return system_to_region, region_name_to_id


def save_region_cache(region_map: dict[str, str],
                      region_ids: dict[str, int] | None = None):
    """Save region mapping to disk.

    Persists the system_id -> region_name map under "regions" (unchanged) and,
    when provided, the region_name -> region_id map under "region_ids" so
    region resolution can be served locally. The extra key is additive; readers
    that only look at "regions" are unaffected."""
    data = {"timestamp": time.time(), "regions": region_map}
    if region_ids:
        data["region_ids"] = region_ids
    atomic_write_json(REGION_CACHE_FILE, data, indent=None)


def load_region_cache() -> dict[str, str] | None:
    """Load the system_id -> region_name map from disk (unchanged contract)."""
    if not os.path.exists(REGION_CACHE_FILE):
        return None
    try:
        with open(REGION_CACHE_FILE, "r") as f:
            data = json.load(f)
        # Region assignments never change, so no expiry needed
        return data.get("regions", {})
    except Exception:
        log.exception(
            "Failed to load region cache %s; discarding", REGION_CACHE_FILE
        )
        return None


def load_region_ids_cache() -> dict[str, int] | None:
    """Load the region_name -> region_id map from disk, if present.

    Returns None when the cache file is missing or has no "region_ids" key
    (e.g. an older cache written before this map was added)."""
    if not os.path.exists(REGION_CACHE_FILE):
        return None
    try:
        with open(REGION_CACHE_FILE, "r") as f:
            data = json.load(f)
        region_ids = data.get("region_ids")
        if isinstance(region_ids, dict) and region_ids:
            # JSON object keys are strings; coerce values to int defensively.
            out: dict[str, int] = {}
            for name, rid in region_ids.items():
                try:
                    out[name] = int(rid)
                except (TypeError, ValueError):
                    continue
            return out or None
        return None
    except Exception:
        log.exception(
            "Failed to load region id cache %s; discarding", REGION_CACHE_FILE
        )
        return None


def get_region_map() -> dict[str, str]:
    """
    Get system_id -> region_name mapping. Uses cache if available.
    Returns {str(system_id): region_name}.
    """
    cached = load_region_cache()
    if cached:
        return cached

    system_to_region, region_name_to_id = _download_region_data()
    if system_to_region:
        save_region_cache(system_to_region, region_name_to_id)
    return system_to_region


def get_region_name_to_id() -> dict[str, int]:
    """
    Get region_name -> region_id mapping for instant/local region resolution.

    Uses the local cache when available. If the cache exists but predates the
    region_ids map (older file), rebuilds the full region data from ESI and
    persists both maps. Returns {} on total failure.
    """
    cached = load_region_ids_cache()
    if cached:
        return cached

    # Either no cache, or an old cache without region_ids — rebuild both maps
    # and persist them together so subsequent calls are local.
    system_to_region, region_name_to_id = _download_region_data()
    if region_name_to_id:
        save_region_cache(system_to_region, region_name_to_id)
    return region_name_to_id


def get_all_region_names() -> list[str]:
    """Return a sorted, de-duplicated list of all region names from the cache.

    Backed by get_region_name_to_id() (local cache first, ESI fallback). The
    map's keys are already unique region names; sorting gives a stable order
    suitable for autocomplete dropdowns."""
    return sorted(get_region_name_to_id().keys())


def search_region(name: str) -> int | None:
    """Resolve a region name to its region_id.

    Tries the local name->id cache first (exact, then case-insensitive match).
    Falls back to esi_auth's /universe/ids/ resolver when the name isn't in the
    cache. Returns None for empty input, unknown regions, or on failure."""
    if not name or not name.strip():
        return None
    query = name.strip()

    name_to_id = get_region_name_to_id()
    if name_to_id:
        # Exact match first.
        if query in name_to_id:
            return name_to_id[query]
        # Case-insensitive fallback.
        folded = query.casefold()
        for rname, rid in name_to_id.items():
            if rname.casefold() == folded:
                return rid

    # Not in the local cache — fall back to ESI /universe/ids/ via esi_auth.
    try:
        import esi_auth
        # resolve_region is an instance method; it only needs esi_post_public
        # (a staticmethod) for region lookups, so a throwaway unauthenticated
        # instance is sufficient and avoids requiring stored credentials.
        # Point it at a non-existent token file so construction performs no
        # token load / network refresh as a side effect.
        resolver = esi_auth.ESIAuth(
            client_id="", client_secret="",
            token_file=os.path.join(app_dir(), "_region_resolver_no_tokens.json"),
        )
        result = resolver.resolve_region(query)
        if result and result.get("id"):
            return int(result["id"])
    except Exception:
        pass
    return None
