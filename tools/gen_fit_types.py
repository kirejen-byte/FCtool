"""
Generate fit_types.json — the bundled typeID -> name/category/slot table used
by type_catalog.TypeCatalog for offline fit resolution.

Downloads three Fuzzwork SDE CSV dumps, joins them, and writes a compact
id->record JSON to the repo root:

    invTypes.csv        typeID, typeName, groupID, published
    invGroups.csv       groupID -> categoryID
    dgmTypeEffects.csv   typeID, effectID  (slot derivation)

Only *published* types relevant to fitting and cargo seeding are emitted
(categories ship=6, module=7, charge=8, drone=18, fighter=87, subsystem=32,
implant=20, deployable=22). Category 20 ("Implant") is bundled in FULL — it
covers attribute implants, skill hardwirings, combat boosters, and cerebral/
skill accelerators; category 22 ("Deployable") covers Mobile Depots and the
like. Both are bundled so a pasted EFT CARGO section (a seeding shopping list)
resolves entirely offline: implants/deployables were previously dropped with
"Unknown item" warnings, which made the seeding tool useless for implant
shopping lists.

Slot is derived from dogma fitting effects for modules/rigs; subsystems get
slot "subsystem" from their category; everything else (ship/charge/drone/
fighter/implant/deployable) has slot null.

IMPORTANT: categories 20 and 22 are deliberately NOT added to
type_catalog.CATEGORY_NAMES, so an implant's or deployable's category_of()
resolves to "other" — which fit_parser routes to cargo, matching the ESI-cache
fallback path (cached entries carry c=None -> also "other"). The DNA serializer
and EFT cargo-routing rely on that "other" classification, so keep 20/22 out of
CATEGORY_NAMES even though they are bundled here.

(History: category 20 used to be excluded except for a ~114-entry combat-booster
carve-out — group 303 gated to the market-group-977 "Booster" subtree via a
fourth CSV, invMarketGroups.csv — added to keep event accelerators and real
implants out of the bundle. That gate and the fourth CSV are gone now that all
of category 20 is bundled.)

Run manually to refresh after a CCP expansion that adds/changes hulls/modules/
implants/deployables:
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

OUT_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "fit_types.json")

# invGroups.categoryID -> our string category. (See module note re: 87.)
CATEGORY_SHIP = 6
CATEGORY_MODULE = 7
CATEGORY_CHARGE = 8
CATEGORY_DRONE = 18
CATEGORY_FIGHTER = 87
CATEGORY_SUBSYSTEM = 32
# Category 20 ("Implant": attribute implants, skill hardwirings, combat boosters,
# cerebral/skill accelerators) and category 22 ("Deployable": Mobile Depot etc.)
# are bundled in FULL so a pasted CARGO seeding list resolves offline. They are
# deliberately NOT in type_catalog.CATEGORY_NAMES, so category_of() reports them
# as "other" and fit_parser routes them to cargo (see the module docstring).
CATEGORY_IMPLANT = 20
CATEGORY_DEPLOYABLE = 22
KEEP_CATEGORIES = {
    CATEGORY_SHIP,
    CATEGORY_MODULE,
    CATEGORY_CHARGE,
    CATEGORY_DRONE,
    CATEGORY_FIGHTER,
    CATEGORY_SUBSYSTEM,
    CATEGORY_IMPLANT,
    CATEGORY_DEPLOYABLE,
}

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
        # Keep the fitting/cargo categories: ships, modules, charges, drones,
        # fighters, subsystems, and all of implants (20) + deployables (22).
        # Category 20/22 resolve to "other" and route to cargo (see docstring).
        if cid not in KEEP_CATEGORIES:
            continue

        # Slot: modules/rigs from dogma effects; subsystems from their category;
        # everything else (ship/charge/drone/fighter/implant/deployable) has no
        # slot.
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
        ("2456", "drone", None),         # Hobgoblin II
        ("12015", "ship", None),         # Muninn
        ("31794", "module", "rig"),      # Small Core Defense Field Extender II
        ("40556", "fighter", None),      # Templar II (categoryID 87)
        ("15463", "other", None),        # Standard Mindflood Booster (combat booster, cat 20)
        ("20499", "other", None),        # High-grade Amulet Alpha (attribute implant, cat 20)
        ("33474", "other", None),        # Mobile Depot (deployable, cat 22)
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
