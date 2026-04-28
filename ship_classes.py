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
GROUP_FRIGATE = 25                # T1 frigates (combat/exploration/mining)
GROUP_ASSAULT_FRIGATE = 324       # T2 assault frigates
GROUP_INTERCEPTOR = 831           # T2 interceptors
GROUP_ELECTRONIC_ATTACK_SHIP = 893  # T2 EAFs

# Tackle = cheap/expendable ships that should NOT trigger loss alerts in
# a main (heavy) fleet. Used by loss_tracker to classify "minor" losses.
TACKLE_GROUP_IDS = {
    GROUP_FRIGATE,
    GROUP_ASSAULT_FRIGATE,
    GROUP_INTERCEPTOR,
    GROUP_ELECTRONIC_ATTACK_SHIP,
    GROUP_DESTROYERS,
    GROUP_TACTICAL_DESTROYERS,
}

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
    49713,  # Zarmazd (Triglavian)
}

LOGISTICS_FRIGATES = {
    37457,  # Deacon
    37459,  # Thalia
    37458,  # Kirin
    37460,  # Scalpel
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

CYNO_SHIPS = {
    11957,  # Falcon
    11969,  # Rapier
    11971,  # Arazu
    11959,  # Pilgrim
    44995,  # Enforcer
}

WEB_SHIPS = {
    17920,  # Bhaalgorn
    17922,  # Ashimmu
    11961,  # Huginn
    11387,  # Hyena
    37454,  # Vigil Fleet Issue
}

HICS = {
    11995,  # Onyx
    12013,  # Broadsword
    12017,  # Devoter
    12021,  # Phobos
}

FAX = {
    37604,  # Apostle
    37606,  # Lif
    37605,  # Minokawa
    37607,  # Ninazu
}

DREADNOUGHTS = {
    19720,  # Revelation
    19722,  # Naglfar
    19724,  # Moros
    19726,  # Phoenix
    52907,  # Zirnitra
    42241,  # Chemosh
    42243,  # Caiman
    45647,  # Vehement
    73790,  # Revelation Navy Issue
    73792,  # Moros Navy Issue
    73793,  # Phoenix Navy Issue
    73787,  # Naglfar Fleet Issue
}

TITANS = {
    671,    # Erebus
    3764,   # Leviathan
    11567,  # Avatar
    23773,  # Ragnarok
    42242,  # Molok
    45649,  # Komodo
}

BLACK_OPS = {
    22428,  # Sin
    22430,  # Widow
    22436,  # Panther
    22440,  # Redeemer
    44996,  # Marshal
}

# ── Convenience unions ──────────────────────────────────────────────────────
ALL_LINKS_COMMAND = COMMAND_SHIPS | COMMAND_DESTROYERS
ALL_LOGISTICS = (LOGISTICS_CRUISERS | LOGISTICS_FRIGATES
                 | T1_LOGI_FRIGATES | T1_LOGI_CRUISERS)
ALL_CYNO = CYNO_SHIPS
ALL_WEBS = WEB_SHIPS
ALL_HICS = HICS
ALL_FAX = FAX
ALL_DREADS = DREADNOUGHTS
ALL_BRIDGE = TITANS | BLACK_OPS

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


def is_tackle(type_id: int) -> bool:
    """Check if a ship is considered 'tackle' (cheap/expendable) for loss tracking.

    Includes: T1 frigates, Assault Frigates, Interceptors, Electronic Attack
    Frigates, T1 Destroyers, Tactical Destroyers.

    Excludes (major): Logistics frigates (1527), Interdictors (541),
    Command Destroyers (1534), Covert Ops (830), Stealth Bombers (834).
    """
    if not type_id:
        return False
    # Explicit exclusions (these share destroyer/frigate hull sizes but are NOT tackle)
    if type_id in INTERDICTORS or type_id in LOGISTICS_FRIGATES:
        return False
    gid = get_group_id(type_id)
    if gid is None:
        return False
    # Interdictors and Command Destroyers happen to inherit from destroyer-ish
    # groups, so also exclude by group ID just in case future types are added.
    if gid in (GROUP_INTERDICTORS, GROUP_COMMAND_DESTROYERS, GROUP_LOGISTICS_FRIGATES):
        return False
    return gid in TACKLE_GROUP_IDS


# ── Ship-type predicate ─────────────────────────────────────────────────────

# Group IDs we already trust as ships
_SHIP_GROUP_IDS_KNOWN: set[int] = {
    GROUP_COMMAND_SHIPS,
    GROUP_COMMAND_DESTROYERS,
    GROUP_LOGISTICS_CRUISERS,
    GROUP_LOGISTICS_FRIGATES,
    GROUP_DESTROYERS,
    GROUP_TACTICAL_DESTROYERS,
    GROUP_INTERDICTORS,
    GROUP_FRIGATE,
    GROUP_ASSAULT_FRIGATE,
    GROUP_INTERCEPTOR,
    GROUP_ELECTRONIC_ATTACK_SHIP,
}

# Cache for ESI group-id lookups (separate from `_group_cache` so it's
# independently monkeypatchable in tests)
_type_group_cache: dict[int, int | None] = {}
_type_group_lock = threading.Lock()


def _fetch_group_id_for_type(type_id: int) -> int | None:
    """Resolve a type_id to its group_id via ESI, with in-memory caching."""
    with _type_group_lock:
        if type_id in _type_group_cache:
            return _type_group_cache[type_id]
    try:
        resp = requests.get(
            f"https://esi.evetech.net/latest/universe/types/{type_id}/",
            timeout=5,
            headers={"User-Agent": "FCTool/1.0 (EVE FC Assistant)"},
        )
        if resp.ok:
            gid = resp.json().get("group_id")
            with _type_group_lock:
                _type_group_cache[type_id] = gid
            return gid
    except Exception:
        pass
    with _type_group_lock:
        _type_group_cache[type_id] = None
    return None


# Hardcoded ship type_ids we already classify (computed once at module load)
_KNOWN_SHIP_TYPE_IDS: set[int] = (
    COMMAND_SHIPS
    | COMMAND_DESTROYERS
    | LOGISTICS_CRUISERS
    | LOGISTICS_FRIGATES
    | T1_LOGI_FRIGATES
    | T1_LOGI_CRUISERS
    | TACTICAL_DESTROYERS
    | INTERDICTORS
    | CYNO_SHIPS
    | WEB_SHIPS
    | HICS
    | FAX
    | DREADNOUGHTS
    | TITANS
    | BLACK_OPS
)


def is_ship_type(type_id: int) -> bool:
    """Return True if type_id is a ship hull, False for structures/drones/etc."""
    if type_id in _KNOWN_SHIP_TYPE_IDS:
        return True
    if classify_ship(type_id) is not None:
        return True
    gid = _fetch_group_id_for_type(type_id)
    if gid is None:
        return False
    return gid in _SHIP_GROUP_IDS_KNOWN
