"""
Generate fit_types.json — the bundled typeID -> name/category/slot table used
by type_catalog.TypeCatalog for offline fit resolution.

Downloads four Fuzzwork SDE CSV dumps, joins them, and writes a compact
id->record JSON to the repo root:

    invTypes.csv        typeID, typeName, groupID, marketGroupID, published
    invGroups.csv       groupID -> categoryID
    dgmTypeEffects.csv   typeID, effectID  (slot derivation)
    invMarketGroups.csv  marketGroupID -> parentGroupID  (booster subtree)

Only *published* types relevant to fittings are emitted (categories ship=6,
module=7, charge=8, drone=18, fighter=87, subsystem=32) plus combat boosters
(category 20 "Implant", group 303 "Booster") — see ``_is_bundled_booster`` for
why boosters need a market-subtree gate rather than a bare group allowlist.
Slot is derived from dogma fitting effects for modules/rigs; subsystems get
slot "subsystem" from their category; ships/charges/drones/fighters/boosters
have slot null.

Run manually to refresh after a CCP expansion that adds/changes hulls/modules:
    py -3.13 tools/gen_fit_types.py
The static type data changes only with patches, so refresh is infrequent.

NOTE: categoryID 87 here is the Fighter inventory category. It is a different
namespace from the SDE flagID 87 (DroneBay) used elsewhere for slot derivation
— do not cross-wire them.
"""
import csv
import io
import json
import os

import requests

# The /csv/ subdirectory is REQUIRED -- the bare .../latest/<file>.csv 404s.
BASE_URL = "https://www.fuzzwork.co.uk/dump/latest/csv/"
INV_TYPES_URL = BASE_URL + "invTypes.csv"
INV_GROUPS_URL = BASE_URL + "invGroups.csv"
DGM_TYPE_EFFECTS_URL = BASE_URL + "dgmTypeEffects.csv"
INV_MARKET_GROUPS_URL = BASE_URL + "invMarketGroups.csv"

OUT_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "fit_types.json")

# invGroups.categoryID -> our string category. (See module note re: 87.)
CATEGORY_SHIP = 6
CATEGORY_MODULE = 7
CATEGORY_CHARGE = 8
CATEGORY_DRONE = 18
CATEGORY_FIGHTER = 87
CATEGORY_SUBSYSTEM = 32
KEEP_CATEGORIES = {
    CATEGORY_SHIP,
    CATEGORY_MODULE,
    CATEGORY_CHARGE,
    CATEGORY_DRONE,
    CATEGORY_FIGHTER,
    CATEGORY_SUBSYSTEM,
}

# Combat boosters ("drugs") are the ONE thing we bundle out of category 20
# (Implant), which is otherwise excluded. They live in group 303 ("Booster"),
# but that group ALSO holds ~350 event Cerebral/Skill Accelerators — so a bare
# group allowlist would bloat the bundle by hundreds of entries. The combat
# drugs are exactly the group-303 types sold under the on-market "Booster"
# market tree (root marketGroupID 977); the accelerators sit off-market or
# under a different market group. A market-subtree gate therefore isolates the
# ~114 real boosters. Category 20 is deliberately NOT added to KEEP_CATEGORIES
# (nor to type_catalog.CATEGORY_NAMES), so a bundled booster's category_of()
# resolves to "other" — which fit_parser routes to cargo, matching the
# ESI-cache fallback path (cached entries carry c=None -> also "other").
CATEGORY_IMPLANT = 20
BOOSTER_GROUP = 303
BOOSTER_MARKET_ROOT = 977

# dogma effectID -> fitting slot, for modules/rigs.
EFFECT_HI_POWER = 12
EFFECT_MED_POWER = 13
EFFECT_LO_POWER = 11
EFFECT_RIG_SLOT = 2663
SLOT_BY_EFFECT = {
    EFFECT_HI_POWER: "high",
    EFFECT_MED_POWER: "med",
    EFFECT_LO_POWER: "low",
    EFFECT_RIG_SLOT: "rig",
}


def _download_csv(url: str) -> list[dict]:
    print(f"Downloading {url} ...")
    resp = requests.get(url, timeout=120)
    resp.raise_for_status()
    # Fuzzwork CSVs are UTF-8 with a BOM; utf-8-sig strips it so the first
    # column key is clean (no BOM-prefixed variant).
    text = resp.content.decode("utf-8-sig")
    return list(csv.DictReader(io.StringIO(text)))


def _as_int(value) -> int | None:
    """Parse an SDE int cell; SDE uses 'None'/'' for NULLs."""
    if value is None:
        return None
    value = value.strip()
    if not value or value == "None":
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _market_parents() -> dict[int, int | None]:
    """marketGroupID -> parentGroupID, for walking the booster market subtree."""
    parents: dict[int, int | None] = {}
    for row in _download_csv(INV_MARKET_GROUPS_URL):
        mid = _as_int(row.get("marketGroupID"))
        if mid is not None:
            parents[mid] = _as_int(row.get("parentGroupID"))
    return parents


def _in_market_subtree(market_group_id: int | None,
                       parents: dict[int, int | None], root: int) -> bool:
    """True if ``market_group_id`` is ``root`` or descends from it."""
    seen: set[int] = set()
    mid = market_group_id
    while mid is not None and mid not in seen:
        if mid == root:
            return True
        seen.add(mid)
        mid = parents.get(mid)
    return False


def _is_bundled_booster(category_id: int | None, group_id: int | None,
                        market_group_id: int | None,
                        parents: dict[int, int | None]) -> bool:
    """Whether a type is a combat booster we bundle from otherwise-excluded
    category 20. See the BOOSTER_* constants for the rationale: group 303 gated
    to the market-group-977 "Booster" subtree, which excludes the ~350 event
    Cerebral/Skill Accelerators that also share group 303."""
    if category_id != CATEGORY_IMPLANT or group_id != BOOSTER_GROUP:
        return False
    return _in_market_subtree(market_group_id, parents, BOOSTER_MARKET_ROOT)


def main() -> None:
    # groupID -> categoryID
    group_to_cat: dict[int, int] = {}
    for row in _download_csv(INV_GROUPS_URL):
        gid = _as_int(row.get("groupID"))
        cid = _as_int(row.get("categoryID"))
        if gid is not None and cid is not None:
            group_to_cat[gid] = cid

    # typeID -> slot, derived from dogma fitting effects.
    slot_by_type: dict[int, str] = {}
    for row in _download_csv(DGM_TYPE_EFFECTS_URL):
        tid = _as_int(row.get("typeID"))
        eid = _as_int(row.get("effectID"))
        if tid is None or eid is None:
            continue
        slot = SLOT_BY_EFFECT.get(eid)
        if slot is not None:
            # First fitting effect wins; a module has exactly one of these.
            slot_by_type.setdefault(tid, slot)

    # marketGroupID -> parentGroupID, used only to gate the combat-booster
    # subtree (see _is_bundled_booster).
    market_parents = _market_parents()

    table: dict[str, dict] = {}
    for row in _download_csv(INV_TYPES_URL):
        published = _as_int(row.get("published"))
        if published != 1:
            continue
        tid = _as_int(row.get("typeID"))
        gid = _as_int(row.get("groupID"))
        name = (row.get("typeName") or "").strip()
        if tid is None or gid is None or not name:
            continue
        cid = group_to_cat.get(gid)
        # Keep the fitting categories, plus combat boosters carved out of the
        # otherwise-excluded category 20 by market subtree.
        if cid not in KEEP_CATEGORIES and not _is_bundled_booster(
            cid, gid, _as_int(row.get("marketGroupID")), market_parents
        ):
            continue

        # Slot: modules/rigs from dogma effects; subsystems from their category;
        # everything else (ship/charge/drone/fighter/booster) has no slot.
        slot = slot_by_type.get(tid)
        if slot is None and cid == CATEGORY_SUBSYSTEM:
            slot = "subsystem"

        table[str(tid)] = {"n": name, "c": cid, "g": gid, "s": slot}

    _atomic_write(table)
    print(f"Wrote {len(table)} fitting types to {OUT_PATH}")
    _sanity_check(table)


def _atomic_write(table: dict) -> None:
    """Write the table to OUT_PATH via temp+replace so a crash mid-write can't
    corrupt the committed artifact (mirrors esi_auth._save_tokens)."""
    tmp_path = f"{OUT_PATH}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(table, f, separators=(",", ":"))
        f.flush()
        try:
            os.fsync(f.fileno())
        except (OSError, AttributeError):
            pass
    os.replace(tmp_path, OUT_PATH)


def _sanity_check(table: dict) -> None:
    """Spot-check known IDs across every category so a bad join is obvious.
    Warnings only — IDs can shift across SDE releases, so don't hard-fail."""
    expectations = [
        ("2048", "module", "low"),       # Damage Control II
        ("12058", "drone", None),        # Hobgoblin II
        ("12015", "ship", None),         # Muninn
        ("31794", "module", "rig"),      # Medium Ancillary Current Router II
        ("37289", "fighter", None),      # a fighter (categoryID 87)
        ("15463", "other", None),        # Standard Mindflood Booster (combat booster)
    ]
    cat_names = {6: "ship", 7: "module", 8: "charge",
                 18: "drone", 87: "fighter", 32: "subsystem"}
    for tid, want_cat, want_slot in expectations:
        entry = table.get(tid)
        if not entry:
            print(f"  WARN: {tid} missing from output")
            continue
        got_cat = cat_names.get(entry["c"], "other")
        ok = got_cat == want_cat and entry["s"] == want_slot
        flag = "OK " if ok else "??? "
        print(f"  {flag}{tid}: n={entry['n']!r} c={got_cat} s={entry['s']} "
              f"(expected c={want_cat} s={want_slot})")


if __name__ == "__main__":
    main()
