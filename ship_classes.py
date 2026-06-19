"""
Ship classification data for EVE Online fleet composition tracking.
Hardcoded type_id sets for known ship categories, with dynamic ESI
fallback for unknown destroyer-class hulls.
"""

import requests
import threading

from esi_constants import ESI_BASE, ESI_HEADERS_JSON as HEADERS

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

# Cyno-capable hull groups (used by CynoCheck). A character flying one of
# these with a Cynosural Field Generator fitted is a cyno alt / lightswitch.
GROUP_HIC = 894                    # Heavy Interdiction Cruiser (normal cyno)
GROUP_FORCE_RECON = 833            # Force Recon Ship (normal OR covert cyno)
GROUP_STRATEGIC_CRUISER = 963      # Strategic Cruiser / T3C (covert cyno)
GROUP_STEALTH_BOMBER = 834         # Stealth Bomber (covert cyno)
GROUP_COVERT_OPS = 830             # Covert Ops (covert cyno)

# Map of cyno-capable victim hull group_id -> short human label. A killmail
# qualifies for CynoCheck when the victim's hull is in one of these groups AND
# a cyno module was fitted in a high slot. See cyno_check.py.
CYNO_LOSS_GROUPS: dict[int, str] = {
    GROUP_HIC: "HIC",
    GROUP_FORCE_RECON: "Force Recon",
    GROUP_STRATEGIC_CRUISER: "Strategic Cruiser",
    GROUP_STEALTH_BOMBER: "Stealth Bomber",
    GROUP_COVERT_OPS: "Covert Ops",
}

# Cynosural Field Generator type_ids. A fit containing either of these in a
# high slot makes the hull a cyno ship for CynoCheck's purposes.
CYNO_MODULE_IDS: set[int] = {
    21096,  # Cynosural Field Generator I (normal cyno)
    28646,  # Covert Cynosural Field Generator I (covert cyno)
}

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
    22448,  # Absolution  (Armor + Information)
    22474,  # Damnation   (Armor + Information)
    22470,  # Nighthawk   (Shield + Information)
    22446,  # Vulture     (Shield + Information)
    22466,  # Astarte     (Armor + Skirmish)
    22442,  # Eos         (Armor + Skirmish)
    22444,  # Sleipnir    (Shield + Skirmish)
    22468,  # Claymore    (Shield + Skirmish)
}

COMMAND_DESTROYERS = {
    37480,  # Bifrost   (Shield + Skirmish)
    37481,  # Pontifex  (Armor + Information)
    37482,  # Stork     (Shield + Information)
    37483,  # Magus     (Armor + Skirmish)
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


def cyno_loss_hull_class(type_id: int) -> str | None:
    """Return the CynoCheck hull-class label if `type_id` is a cyno-capable
    hull (HIC / Force Recon / Strategic Cruiser / Stealth Bomber / Covert Ops),
    else None.

    Resolves the hull's group via get_group_id (ESI + cache) rather than a
    hard-coded type-id roster, so newly released hulls in these groups are
    covered automatically. A falsy/None type_id yields None.
    """
    if not type_id:
        return None
    gid = get_group_id(type_id)
    if gid is None:
        return None
    return CYNO_LOSS_GROUPS.get(gid)


def has_cyno_module(item_type_ids) -> bool:
    """Return True if any type_id in `item_type_ids` is a Cynosural Field
    Generator (normal or covert).

    `item_type_ids` is any iterable of type_ids (ints). Non-int / None entries
    are ignored. A None or empty iterable yields False.
    """
    if not item_type_ids:
        return False
    for tid in item_type_ids:
        if tid in CYNO_MODULE_IDS:
            return True
    return False


def is_defender(type_id: int) -> bool:
    """Check if a ship is a destroyer-class hull but NOT an interdictor."""
    if type_id in INTERDICTORS:
        return False
    if type_id in TACTICAL_DESTROYERS:
        return True
    gid = get_group_id(type_id)
    return gid == GROUP_DESTROYERS


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

# Canonical EVE Ship category (category_id 6) groups. Hardcoded so d-scan
# filtering doesn't depend on a per-type ESI category lookup.
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
    # Combat cruisers / battlecruisers / battleships
    26, 358, 833, 906, 963, 419, 1201, 27, 900, 898,
    # Cyno-capable covert hulls (Covert Ops 830, Stealth Bomber 834);
    # Force Recon 833, T3C 963, HIC 894 already covered above/below.
    GROUP_COVERT_OPS, GROUP_STEALTH_BOMBER,
    # Capital classes
    547, 1538, 485, 4594, 513, 883, 902, 659, 30,
    # Industrial / mining / hauling (visible on d-scan; treat as ships)
    28, 380, 463, 543, 941, 1283,
    # Faction / utility / event
    894, 1202, 1972,
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
        from rate_limiter import rate_limit
        rate_limit("esi")
        resp = requests.get(
            f"{ESI_BASE}/universe/types/{type_id}/",
            timeout=5,
            headers=HEADERS,
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
