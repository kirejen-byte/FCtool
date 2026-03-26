"""
Ship classification data for EVE Online fleet composition tracking.
Hardcoded type_id sets for known ship categories, with dynamic ESI
fallback for unknown destroyer-class hulls.
"""

import requests
import threading

# ── EVE Online Group IDs (from SDE) ─────────────────────────────────────────
GROUP_COMMAND_SHIPS = 540
GROUP_COMMAND_DESTROYERS = 1534
GROUP_LOGISTICS_CRUISERS = 832
GROUP_LOGISTICS_FRIGATES = 1527
GROUP_DESTROYERS = 420
GROUP_TACTICAL_DESTROYERS = 1305  # T3D
GROUP_INTERDICTORS = 541

# ── Hardcoded type_id sets ──────────────────────────────────────────────────

COMMAND_SHIPS = {
    22448,  # Vulture
    24690,  # Absolution
    22468,  # Claymore
    22444,  # Sleipnir
    24692,  # Damnation
    22470,  # Nighthawk
    24688,  # Eos
    22466,  # Astarte
}

COMMAND_DESTROYERS = {
    37483,  # Bifrost
    37481,  # Stork
    37482,  # Pontifex
    37480,  # Magus
}

LOGISTICS_CRUISERS = {
    11987,  # Guardian
    11989,  # Oneiros
    11978,  # Scimitar
    11985,  # Basilisk
}

LOGISTICS_FRIGATES = {
    37457,  # Deacon
    37455,  # Thalia
    37456,  # Kirin
    37454,  # Scalpel
}

T1_LOGI_FRIGATES = {
    590,    # Inquisitor
    592,    # Navitas
    582,    # Bantam
    598,    # Burst
}

T1_LOGI_CRUISERS = {
    72811,  # Rodiva
    625,    # Augoror
    634,    # Exequror
    620,    # Osprey
    631,    # Scythe
}

TACTICAL_DESTROYERS = {
    34562,  # Svipul
    34828,  # Jackdaw
    34317,  # Confessor
    35683,  # Hecate
}

INTERDICTORS = {
    22456,  # Sabre
    22452,  # Flycatcher
    22460,  # Eris
    22464,  # Heretic
}

# ── Convenience unions ──────────────────────────────────────────────────────
ALL_LINKS_COMMAND = COMMAND_SHIPS | COMMAND_DESTROYERS
ALL_LOGISTICS = (LOGISTICS_CRUISERS | LOGISTICS_FRIGATES
                 | T1_LOGI_FRIGATES | T1_LOGI_CRUISERS)

# ── Dynamic group_id resolution cache ───────────────────────────────────────
_group_cache: dict[int, int | None] = {}
_group_cache_lock = threading.Lock()

ESI_BASE = "https://esi.evetech.net/latest"
HEADERS = {"User-Agent": "FCTool/1.0", "Accept": "application/json"}


def get_group_id(type_id: int) -> int | None:
    """Resolve a type_id to its group_id via ESI, with thread-safe caching."""
    with _group_cache_lock:
        if type_id in _group_cache:
            return _group_cache[type_id]

    try:
        from rate_limiter import rate_limit
        rate_limit("esi")
        resp = requests.get(
            f"{ESI_BASE}/universe/types/{type_id}/",
            timeout=5, headers=HEADERS
        )
        if resp.ok:
            gid = resp.json().get("group_id")
            with _group_cache_lock:
                _group_cache[type_id] = gid
            return gid
    except Exception:
        pass

    with _group_cache_lock:
        _group_cache[type_id] = None
    return None


def classify_ship(type_id: int) -> str | None:
    """Return the role category string for a ship type_id, or None."""
    if type_id in COMMAND_SHIPS:
        return "command_ship"
    if type_id in COMMAND_DESTROYERS:
        return "command_destroyer"
    if type_id in LOGISTICS_CRUISERS:
        return "logi_cruiser"
    if type_id in LOGISTICS_FRIGATES:
        return "logi_frigate"
    if type_id in T1_LOGI_FRIGATES:
        return "t1_logi_frigate"
    if type_id in T1_LOGI_CRUISERS:
        return "t1_logi_cruiser"
    if type_id in TACTICAL_DESTROYERS:
        return "t3_destroyer"
    if type_id in INTERDICTORS:
        return "interdictor"
    return None


def is_defender(type_id: int) -> bool:
    """Check if a ship is a destroyer-class hull but NOT an interdictor."""
    if type_id in INTERDICTORS:
        return False
    if type_id in TACTICAL_DESTROYERS:
        return True
    gid = get_group_id(type_id)
    return gid == GROUP_DESTROYERS


def is_links_command(type_id: int) -> bool:
    """Check if a ship is a command ship or command destroyer."""
    return type_id in ALL_LINKS_COMMAND


def is_logistics(type_id: int) -> bool:
    """Check if a ship is any logistics ship (T1 or T2, frig or cruiser)."""
    return type_id in ALL_LOGISTICS
