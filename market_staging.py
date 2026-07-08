"""Name-first staging resolver for the Market Scanner.

Turns a staging SYSTEM NAME into the ids the scanner needs — so FCs never copy
raw ids. Resolution is LOCAL-FIRST:

* ``system_id`` — from the bundled static table (``system_coords`` /
  seeded ``system_cache``); instant and offline. Public ``/universe/ids/`` is
  used only as a last resort (e.g. an EVE-Scout WH name not in the K-space
  table).
* ``region_id`` — from the same bundled table (``system_coords`` carries
  system->region). Public ``/universe/systems/`` + ``/universe/constellations/``
  is used only when the table lacks it.
* ``stations`` — the system's NPC station ids + names. Always public ESI
  (``/universe/systems/{id}`` for the id list, ``/universe/names/`` for names),
  since station membership isn't in the local coordinate table.

A system's region and station set never change, so every resolution is cached
FOREVER in a small ``app_dir`` JSON (``market_staging_cache.json``), keyed by
system_id. For a system whose id resolves LOCALLY (any bundled K-space system —
the normal case) a repeat resolve therefore does ZERO network. Off-table names
(e.g. EVE-Scout wormhole systems) are the exception: because the cache key is
the system_id, the name -> id step re-POSTs ``/universe/ids/`` on every resolve;
only the region/station fetches are saved by the cache. All network here is
PUBLIC ESI (no auth, no scopes).

The concrete ``_PublicAdapter`` does the real ``requests`` calls; tests inject a
fake adapter exposing the same four methods so nothing touches the network.
"""

from __future__ import annotations

import json
import os

import requests

from app_io import atomic_write_json
from app_log import get_logger
from app_path import app_dir
from esi_constants import ESI_BASE, ESI_HEADERS as HEADERS
from rate_limiter import rate_limit

log = get_logger(__name__)

CACHE_FILE = os.path.join(app_dir(), "market_staging_cache.json")
CACHE_VERSION = 1


# ── Public-ESI network seam (tests inject a fake adapter) ────────────────────
def _public_get(path: str):
    try:
        rate_limit("esi")
        resp = requests.get(f"{ESI_BASE}{path}", headers=HEADERS, timeout=10)
        if resp.ok:
            return resp.json()
    except Exception:
        log.exception("[market_staging] public GET %s failed", path)
    return None


def _public_post(path: str, body):
    try:
        rate_limit("esi")
        resp = requests.post(
            f"{ESI_BASE}{path}", json=body,
            headers={**HEADERS, "Content-Type": "application/json"}, timeout=10)
        if resp.ok:
            return resp.json()
    except Exception:
        log.exception("[market_staging] public POST %s failed", path)
    return None


class _PublicAdapter:
    """Default public-ESI fetcher. No auth. One shared instance module-wide."""

    def get_system(self, system_id: int):
        return _public_get(f"/universe/systems/{int(system_id)}/")

    def get_constellation(self, constellation_id: int):
        return _public_get(f"/universe/constellations/{int(constellation_id)}/")

    def post_names(self, ids):
        return _public_post("/universe/names/", list(ids))

    def post_ids(self, names):
        return _public_post("/universe/ids/", list(names))


_DEFAULT_ADAPTER = _PublicAdapter()


# ── Local-first id resolution (no network) ───────────────────────────────────
def local_system_id(name: str) -> int | None:
    """System name -> system_id from the bundled static table (instant, offline).

    Tries ``system_coords`` (exact, case-insensitive) first, then the seeded
    ``system_cache`` name map. Returns None when the name isn't in either local
    source (the caller then falls back to public ``/universe/ids/``)."""
    if not name or not name.strip():
        return None
    q = name.strip()
    try:
        import system_coords
        sid = system_coords.resolve_name(q)
        if sid:
            return int(sid)
    except Exception:
        log.exception("[market_staging] system_coords name lookup failed")
    try:
        import system_cache
        names = system_cache.get_system_names() or {}
        if q in names:
            return int(names[q])
        folded = q.casefold()
        for nm, sid in names.items():
            if nm.casefold() == folded:
                return int(sid)
    except Exception:
        log.exception("[market_staging] system_cache name lookup failed")
    return None


def local_region_id(system_id) -> int | None:
    """System_id -> region_id from the bundled static table (instant, offline).

    Returns None when the table lacks the system (e.g. J-space, or no table
    shipped) — the caller then resolves the region via public ESI."""
    if not system_id:
        return None
    try:
        import system_coords
        rid = system_coords.get_region_id(int(system_id))
        return int(rid) if rid else None
    except Exception:
        log.exception("[market_staging] system_coords region lookup failed")
        return None


def local_ids(name: str) -> tuple[int | None, int | None]:
    """(system_id, region_id) from local data alone — for an instant, no-network
    id auto-fill the moment a system is chosen. Either element may be None."""
    sid = local_system_id(name)
    rid = local_region_id(sid) if sid else None
    return sid, rid


# ── Forever-cache (system_id -> resolution) ──────────────────────────────────
def _load_cache() -> dict:
    """Load the {str(system_id): resolution} map. {} when missing, corrupt, or
    written by a different schema version (a future CACHE_VERSION bump thereby
    auto-invalidates old files)."""
    if not os.path.exists(CACHE_FILE):
        return {}
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if data.get("version") != CACHE_VERSION:
            return {}
        systems = data.get("systems")
        return systems if isinstance(systems, dict) else {}
    except Exception:
        log.exception(
            "[market_staging] cache load failed %s; discarding", CACHE_FILE)
        return {}


def get_cached(system_id) -> dict | None:
    """Return a previously-resolved resolution for ``system_id`` from disk, or
    None. Network-free — for priming the UI from a saved staging system without
    a re-fetch."""
    if not system_id:
        return None
    hit = _load_cache().get(str(int(system_id)))
    return hit if isinstance(hit, dict) else None


def get_cached_stations(system_id) -> list | None:
    """Station list ``[{"id","name"}, ...]`` for a previously-resolved system,
    read from the on-disk cache (network-free). Returns ``None`` when the system
    was never resolved (cache MISS) and ``[]`` for a resolved but station-less
    system — so a caller can tell 'unknown' apart from 'known to have no
    stations'. For priming/restoring the station picker from a saved staging
    system without a re-fetch."""
    hit = get_cached(system_id)
    if not isinstance(hit, dict):
        return None
    stations = hit.get("stations")
    return stations if isinstance(stations, list) else []


def _save_cache(systems: dict) -> None:
    """Persist the resolution map (best-effort; a write failure is logged, not
    raised — a missing cache just means the next resolve re-fetches)."""
    try:
        atomic_write_json(
            CACHE_FILE, {"version": CACHE_VERSION, "systems": systems})
    except Exception:
        log.exception("[market_staging] cache save failed %s", CACHE_FILE)


def _name_stations(station_ids, adapter) -> list[dict]:
    """Resolve NPC station ids -> [{"id","name"}] via public /universe/names/,
    preserving id order. Empty list on no ids / failure. A station that fails to
    resolve keeps a synthetic 'Station <id>' label so its id is still pickable."""
    ids = [int(s) for s in (station_ids or []) if isinstance(s, int)]
    if not ids:
        return []
    data = adapter.post_names(ids)
    names: dict[int, str] = {}
    if isinstance(data, list):
        for entry in data:
            if isinstance(entry, dict):
                eid = entry.get("id")
                nm = entry.get("name")
                if isinstance(eid, int) and isinstance(nm, str):
                    names[eid] = nm
    return [{"id": sid, "name": names.get(sid) or f"Station {sid}"}
            for sid in ids]


def resolve_staging(name: str, adapter=None, *, use_cache: bool = True) -> dict | None:
    """Resolve a staging system NAME to a resolution dict:

        {"name", "system_id", "region_id", "constellation_id",
         "stations": [{"id", "name"}, ...]}

    ``system_id`` + ``region_id`` come from local data when available (no
    network); ``stations`` always require public ESI. The whole resolution is
    cached forever keyed by system_id, so a repeat call for a system whose id
    resolves locally (any bundled K-space system) does ZERO network. An
    OFF-TABLE name (e.g. an EVE-Scout WH system) still re-POSTs
    ``/universe/ids/`` on each call to find the id before the cache can answer —
    only steps 3-4 below are saved for those. Returns None only when the name
    cannot be resolved to a system id at all. Never raises.
    """
    if not name or not name.strip():
        return None
    name = name.strip()
    adapter = adapter or _DEFAULT_ADAPTER

    # 1) system_id: local first, then public /universe/ids/.
    system_id = local_system_id(name)
    if not system_id:
        data = adapter.post_ids([name])
        if isinstance(data, dict):
            systems = data.get("systems") or []
            folded = name.casefold()
            for entry in systems:
                if (isinstance(entry, dict) and entry.get("id")
                        and str(entry.get("name", "")).casefold() == folded):
                    system_id = int(entry["id"])
                    break
            if not system_id and systems and isinstance(systems[0], dict):
                if systems[0].get("id"):
                    system_id = int(systems[0]["id"])
    if not system_id:
        return None

    # 2) cache hit? A stored resolution is immutable, so a full hit does ZERO
    #    network (this is the "second select = no calls" fast path).
    cache = _load_cache() if use_cache else {}
    key = str(system_id)
    hit = cache.get(key)
    if isinstance(hit, dict) and "region_id" in hit and "stations" in hit:
        return hit

    # 3) region_id: local first; else systems -> constellation traversal.
    region_id = local_region_id(system_id) or 0
    constellation_id = 0
    sysinfo = None
    if not region_id:
        sysinfo = adapter.get_system(system_id)
        if isinstance(sysinfo, dict):
            constellation_id = int(sysinfo.get("constellation_id") or 0)
            if constellation_id:
                cinfo = adapter.get_constellation(constellation_id)
                if isinstance(cinfo, dict):
                    region_id = int(cinfo.get("region_id") or 0)

    # 4) stations: always ESI. Reuse the systems payload if we already fetched
    #    it above for the region traversal.
    if sysinfo is None:
        sysinfo = adapter.get_system(system_id)
    station_ids: list[int] = []
    resolved_name = name
    if isinstance(sysinfo, dict):
        station_ids = [s for s in (sysinfo.get("stations") or [])
                       if isinstance(s, int)]
        if not constellation_id:
            constellation_id = int(sysinfo.get("constellation_id") or 0)
        resolved_name = sysinfo.get("name") or name
    stations = _name_stations(station_ids, adapter)

    resolution = {
        "name": resolved_name,
        "system_id": int(system_id),
        "region_id": int(region_id or 0),
        "constellation_id": int(constellation_id or 0),
        "stations": stations,
    }

    # 5) persist forever.
    if use_cache:
        cache[key] = resolution
        _save_cache(cache)
    return resolution
