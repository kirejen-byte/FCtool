"""Generate ``fit_dogma.json.gz`` -- the bundled dogma table the fit simulator reads.

Source of truth: CCP's new-SDE JSONL export (the same zip ``gen_map_layout.py``
uses, redistributable under the CCP Developer License).  Seven members are
streamed straight out of the zip -- nothing is ever extracted to disk:

    _sde.jsonl            build number stamped into the table
    groups.jsonl          groupID -> categoryID
    types.jsonl           typeID -> groupID (152 MB; streamed, never held whole)
    typeDogma.jsonl       per-type attribute values + effect ids
    dogmaEffects.jsonl    effectCategoryID + modifierInfo
    dogmaAttributes.jsonl defaultValue / stackable / highIsGood (+ names)
    dbuffCollections.jsonl command-burst warfare buffs

FILTER (spec Appendix A.4 -- the recipe that measured 199.5 KiB):

    kept types      = every id in fit_types.json
                      + every category-16 type (skills)
                      + Command Burst modules (group 1770)
                      + the combat burst charges (groups 1769/1772/1773/1774)
                      + Mindlink implants (category 20, name contains "Mindlink")
    kept effects    = every effect attached to a kept type
    kept attributes = every attribute a KEPT modifier row or dbuff row references
                      + ATTR_WHITELIST_NAMES (the v1 stat set)

A type's ``a`` list carries only kept attributes whose value DIFFERS from the
attribute default, so the table stores the type's own values and the reader
fills defaults from ``attrs``.

Modifier-row policy (Appendix A.8 items 4/5): a row whose ``func`` is
``EffectStopper`` (it carries no ``modifiedAttributeID``/``operation`` at all) or
whose ``operation`` is outside the eight real dogma op codes is DROPPED -- it
carries no attribute semantics.  The EFFECT itself is always kept, possibly with
an empty ``m``, so the engine can still see that the effect exists and report it.

SKILL-EFFECT OVERRIDES: a handful of skill effects have no ``modifierInfo`` in
the SDE at all (CCP never published one -- pyfa hand-writes them), so a pure
``modifierInfo`` engine applies nothing and the numbers those skills feed come
out low.  ``tools/dogma_overrides.py`` carries curated rows for them, spliced in
here after filtering.  The gate is total: a modifier-less effect on a
category-16 type that is in NEITHER ``SKILL_EFFECT_OVERRIDES`` nor
``SKILL_EFFECTS_IGNORED`` aborts the build with exit 2, so a gap a future SDE
introduces gets triaged rather than shipped.

Output encoding is the contract in ``dogma_data.py`` -- that module is the only
reader, and its docstring is authoritative.  Compact separators, gzip level 9,
atomic temp+replace write.

Usage:
  py -3.12 tools/gen_fit_dogma.py --sde-zip tools/_cache/sde.zip
  py -3.12 tools/gen_fit_dogma.py --download

Exit codes: 0 ok, 2 a size budget was exceeded (the file is still written so the
breach can be inspected) or an untriaged modifier-less skill effect was found,
1 anything fatal.
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import sys
import tracemalloc
import zipfile
from pathlib import Path
from typing import Iterable, NamedTuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
import dogma_overrides  # noqa: E402  (sibling module; the insert above is what finds it)

SDE_ZIP_URL = "https://developers.eveonline.com/static-data/eve-online-static-data-latest-jsonl.zip"
SDE_BUILD_URL = "https://developers.eveonline.com/static-data/tranquility/latest.jsonl"
REPO_ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = Path(__file__).resolve().parent / "_cache"

#: Zip members, in the order the build consumes them.
MEMBER_BUILD = "_sde.jsonl"
MEMBER_GROUPS = "groups.jsonl"
MEMBER_TYPES = "types.jsonl"
MEMBER_TYPE_DOGMA = "typeDogma.jsonl"
MEMBER_EFFECTS = "dogmaEffects.jsonl"
MEMBER_ATTRIBUTES = "dogmaAttributes.jsonl"
MEMBER_DBUFFS = "dbuffCollections.jsonl"

# --------------------------------------------------------------------------
# budgets (spec Appendix A.8 item 1 -- ~1.7x headroom over the measured table).
# Read through the module globals so a test can monkeypatch them.
# --------------------------------------------------------------------------
GZIP_BUDGET_BYTES = 350_000
TEXT_BUDGET_BYTES = 2_500_000

# --------------------------------------------------------------------------
# filter constants
# --------------------------------------------------------------------------
SKILL_CATEGORY_ID = 16
IMPLANT_CATEGORY_ID = 20
BURST_MODULE_GROUP_ID = 1770
BURST_CHARGE_GROUP_IDS = frozenset({1769, 1772, 1773, 1774})
MINDLINK_NAME_NEEDLE = "mindlink"

#: The eight real dogma operation codes: -1 preAssign, 0 preMul, 2 modAdd,
#: 3 modSub, 4 postMul, 5 postDiv, 6 postPercent, 7 postAssign.  (1 preDiv is
#: reserved and unused by the SDE.)  A modifier row carrying anything else --
#: the lone ``9`` on skillEffect, and the ``None``s on EffectStopper -- is
#: dropped: it has no attribute semantics for the engine.
VALID_OPERATIONS = frozenset({-1, 0, 2, 3, 4, 5, 6, 7})

#: Modifier funcs that carry no attribute at all and are dropped row-wise.
DROPPED_MODIFIER_FUNCS = frozenset({"EffectStopper"})

#: ``dbuffCollections.operationName`` -> dogma op code.  Every name the current
#: SDE uses is mapped; an UNKNOWN name is fatal rather than silently dropped,
#: because a buff whose operation we cannot express would be applied wrongly.
DBUFF_OPERATIONS = {
    "PreAssignment": -1,
    "ModAdd": 2,
    "PostMul": 4,
    "PostPercent": 6,
    "PostAssignment": 7,
}

#: ``dbuffCollections`` sub-list -> the ``kind`` tag the table stores, in the
#: order rows are emitted.
DBUFF_MODIFIER_KINDS = (
    ("itemModifiers", "item"),
    ("locationModifiers", "location"),
    ("locationGroupModifiers", "location_group"),
    ("locationRequiredSkillModifiers", "location_skill"),
)

#: The v1 stat set, by SDE attribute NAME (ids move between builds, names do
#: not).  Almost every attribute in the table arrives via a modifier reference;
#: this whitelist is what makes the stats the engine READS (rather than
#: modifies) present with their defaults.  An unresolvable name is fatal -- a
#: silently-missing stat attribute would show up as a wrong number, not an error.
#: Appendix A.8 item 2: rate of fire is ``speed`` (51), there is no ``metaLevel``
#: attribute in this build, and ``barrageFalloff`` (328) must be carried.
ATTR_WHITELIST_NAMES = (
    # hit points
    "hp", "shieldCapacity", "armorHP",
    # resonances (= 1 - resist).  Hull resonances are stackable, shield/armor
    # are not -- the engine reads that off attr_info, never off the name.
    "emDamageResonance", "thermalDamageResonance",
    "kineticDamageResonance", "explosiveDamageResonance",
    "armorEmDamageResonance", "armorThermalDamageResonance",
    "armorKineticDamageResonance", "armorExplosiveDamageResonance",
    "shieldEmDamageResonance", "shieldThermalDamageResonance",
    "shieldKineticDamageResonance", "shieldExplosiveDamageResonance",
    "hullEmDamageResonance", "hullThermalDamageResonance",
    "hullKineticDamageResonance", "hullExplosiveDamageResonance",
    # damage + weapon
    "emDamage", "thermalDamage", "kineticDamage", "explosiveDamage",
    "damageMultiplier", "missileDamageMultiplier",
    "speed",            # rate of fire, turrets AND launchers (A.7)
    "maxRange", "falloff", "barrageFalloff", "trackingSpeed",
    "maxVelocity", "explosionDelay", "aoeVelocity", "aoeCloudSize",
    "optimalSigRadius",
    # drones / capacity
    "droneBandwidth", "droneBandwidthUsed", "maxActiveDrones", "droneCapacity",
    "capacity", "chargeSize", "chargeRate", "volume",
    # warfare buffs (the links path)
    "warfareBuff1ID", "warfareBuff1Value", "warfareBuff2ID", "warfareBuff2Value",
    "warfareBuff3ID", "warfareBuff3Value", "warfareBuff4ID", "warfareBuff4Value",
    "warfareBuff1Multiplier", "warfareBuff2Multiplier",
    "warfareBuff3Multiplier", "warfareBuff4Multiplier",
    "buffDuration", "mindlinkBonus",
    # skills
    "skillLevel",
    "requiredSkill1", "requiredSkill2", "requiredSkill3",
    "requiredSkill4", "requiredSkill5", "requiredSkill6",
    "requiredSkill1Level", "requiredSkill2Level", "requiredSkill3Level",
    "requiredSkill4Level", "requiredSkill5Level", "requiredSkill6Level",
    # hull / fitting context
    "mass", "signatureRadius", "duration", "reloadTime",
    "launcherSlotsLeft", "turretSlotsLeft", "power", "cpu",
    "shieldRechargeRate", "techLevel", "radius",
)


class TypeIndex(NamedTuple):
    """What one streaming pass over ``types.jsonl`` yields."""
    group: dict          # type_id -> group_id
    category: dict       # type_id -> category_id
    skills: set          # category-16 type ids
    burst_modules: set   # group 1770
    burst_charges: set   # groups 1769/1772/1773/1774
    mindlinks: set       # category 20, name contains "Mindlink"


# --------------------------------------------------------------------------
# JSONL streaming
# --------------------------------------------------------------------------

def iter_jsonl(lines: Iterable) -> Iterable[dict]:
    """Parse a JSONL stream. Accepts str or bytes lines (``json.loads`` takes
    both), skips blank lines, and tolerates the CRLF the SDE members use."""
    for line in lines:
        line = line.strip()
        if line:
            yield json.loads(line)


def _en(value) -> str:
    """The English string out of an SDE localized-name object."""
    if isinstance(value, dict):
        return str(value.get("en") or "")
    return "" if value is None else str(value)


# --------------------------------------------------------------------------
# stage 1: the type universe
# --------------------------------------------------------------------------

def load_group_categories(lines: Iterable) -> dict:
    """``groupID -> categoryID`` from ``groups.jsonl``."""
    out = {}
    for rec in iter_jsonl(lines):
        gid, cid = rec.get("_key"), rec.get("categoryID")
        if isinstance(gid, int) and isinstance(cid, int):
            out[gid] = cid
    return out


def index_types(lines: Iterable, group_categories: dict) -> TypeIndex:
    """Stream ``types.jsonl`` into the per-type group/category maps plus the four
    membership sets the filter needs.

    Names are inspected (Mindlinks are identified by name) but never retained --
    the table carries no names, and holding 53k of them would be pure waste."""
    group, category = {}, {}
    skills, burst_modules, burst_charges, mindlinks = set(), set(), set(), set()
    for rec in iter_jsonl(lines):
        tid = rec.get("_key")
        gid = rec.get("groupID")
        if not isinstance(tid, int) or not isinstance(gid, int):
            continue
        cid = group_categories.get(gid, 0)
        group[tid] = gid
        category[tid] = cid
        if cid == SKILL_CATEGORY_ID:
            skills.add(tid)
        if gid == BURST_MODULE_GROUP_ID:
            burst_modules.add(tid)
        if gid in BURST_CHARGE_GROUP_IDS:
            burst_charges.add(tid)
        if cid == IMPLANT_CATEGORY_ID and MINDLINK_NAME_NEEDLE in _en(rec.get("name")).lower():
            mindlinks.add(tid)
    return TypeIndex(group, category, skills, burst_modules, burst_charges, mindlinks)


def select_types(index: TypeIndex, fit_type_ids: Iterable[int]) -> frozenset:
    """The kept-type id set (Appendix A.4).

    Ids the SDE does not know are dropped -- the table could not carry their
    group/category, and ``dogma_data``'s type accessors require both. See
    :func:`unknown_fit_type_ids` for surfacing which ids that was."""
    wanted = set(int(t) for t in fit_type_ids)
    wanted |= index.skills | index.burst_modules | index.burst_charges | index.mindlinks
    return frozenset(t for t in wanted if t in index.group)


def unknown_fit_type_ids(index: TypeIndex, fit_type_ids: Iterable[int]) -> tuple:
    """``--fit-types`` ids that ``types.jsonl`` does not define at all.

    ``select_types`` drops these silently (it has to -- the table cannot carry
    a group/category for an id the SDE never defines); this is what lets the
    caller report them instead of losing the count."""
    return tuple(sorted(t for t in (int(t) for t in fit_type_ids) if t not in index.group))


# --------------------------------------------------------------------------
# stage 2: dogma
# --------------------------------------------------------------------------

def load_type_dogma(lines: Iterable, kept_types: frozenset) -> dict:
    """``type_id -> ({attr_id: value}, (effect_id, ...))`` for kept types only."""
    out = {}
    for rec in iter_jsonl(lines):
        tid = rec.get("_key")
        if not isinstance(tid, int) or tid not in kept_types:
            continue
        attrs = {}
        for row in rec.get("dogmaAttributes") or ():
            aid = row.get("attributeID")
            if isinstance(aid, int):
                attrs[aid] = float(row.get("value", 0.0))
        effects = tuple(row["effectID"] for row in (rec.get("dogmaEffects") or ())
                        if isinstance(row.get("effectID"), int))
        out[tid] = (attrs, effects)
    return out


def normalise_modifiers(modifier_info) -> list:
    """``modifierInfo`` -> the table's flat 7-column rows.

    ``[domain, func, modifiedAttr, modifyingAttr, operation, skillTypeID|None,
    groupID|None]``.  Rows with no attribute semantics are dropped (see
    ``DROPPED_MODIFIER_FUNCS`` / ``VALID_OPERATIONS``); the caller keeps the
    effect regardless, so an effect whose every row is dropped survives with an
    empty list and the engine can still report it as unmodeled."""
    rows = []
    for mod in modifier_info or ():
        func = mod.get("func")
        if func in DROPPED_MODIFIER_FUNCS:
            continue
        op = mod.get("operation")
        if op not in VALID_OPERATIONS:
            continue
        modified = mod.get("modifiedAttributeID")
        modifying = mod.get("modifyingAttributeID")
        if not isinstance(modified, int) or not isinstance(modifying, int):
            continue
        skill_id = mod.get("skillTypeID")
        group_id = mod.get("groupID")
        rows.append([
            str(mod.get("domain") or ""),
            str(func or ""),
            modified,
            modifying,
            int(op),
            int(skill_id) if isinstance(skill_id, int) else None,
            int(group_id) if isinstance(group_id, int) else None,
        ])
    return rows


def load_effects(lines: Iterable, kept_effects: set) -> dict:
    """``{"<effect id>": {"cat": n, "m": [row, ...]}}`` for kept effects."""
    out = {}
    for rec in iter_jsonl(lines):
        eid = rec.get("_key")
        if not isinstance(eid, int) or eid not in kept_effects:
            continue
        out[str(eid)] = {
            "cat": int(rec.get("effectCategoryID") or 0),
            "m": normalise_modifiers(rec.get("modifierInfo")),
        }
    return out


def load_attributes(lines: Iterable) -> dict:
    """``attr_id -> (name, defaultValue, stackable, highIsGood)``.

    All three flag/value fields are present on every SDE record (Appendix A.3),
    so the encoding is total and nothing is ever defaulted here."""
    out = {}
    for rec in iter_jsonl(lines):
        aid = rec.get("_key")
        if not isinstance(aid, int):
            continue
        out[aid] = (
            str(rec.get("name") or ""),
            float(rec.get("defaultValue") or 0.0),
            bool(rec.get("stackable")),
            bool(rec.get("highIsGood")),
        )
    return out


def resolve_whitelist(attributes: dict) -> set:
    """``ATTR_WHITELIST_NAMES`` -> attribute ids. Fatal on an unknown name."""
    by_name = {info[0]: aid for aid, info in attributes.items()}
    ids, missing = set(), []
    for name in ATTR_WHITELIST_NAMES:
        aid = by_name.get(name)
        if aid is None:
            missing.append(name)
        else:
            ids.add(aid)
    if missing:
        raise SystemExit(
            "FATAL: ATTR_WHITELIST_NAMES contains name(s) this SDE build does not "
            f"define: {', '.join(missing)} -- an attribute renamed by CCP must be "
            "fixed in the whitelist, never silently dropped")
    return ids


def normalise_dbuffs(lines: Iterable) -> dict:
    """``{"<buff id>": {"agg": ..., "op": <int>, "m": [[kind, attr, group, skill], ...]}}``.

    Note the SDE's dbuff sub-records use ``skillID`` (not ``skillTypeID``) and a
    string ``aggregateMode``/``operationName`` (Appendix A.8 item 7)."""
    out = {}
    for rec in iter_jsonl(lines):
        bid = rec.get("_key")
        if not isinstance(bid, int):
            continue
        op_name = rec.get("operationName")
        if op_name not in DBUFF_OPERATIONS:
            raise SystemExit(
                f"FATAL: dbuff {bid} has operationName {op_name!r}, which is not in "
                f"DBUFF_OPERATIONS {sorted(DBUFF_OPERATIONS)} -- map it to its dogma "
                "op code before regenerating")
        rows = []
        for source, kind in DBUFF_MODIFIER_KINDS:
            for mod in rec.get(source) or ():
                attr = mod.get("dogmaAttributeID")
                if not isinstance(attr, int):
                    continue
                group_id = mod.get("groupID")
                skill_id = mod.get("skillID")
                rows.append([
                    kind,
                    attr,
                    int(group_id) if isinstance(group_id, int) else None,
                    int(skill_id) if isinstance(skill_id, int) else None,
                ])
        out[str(bid)] = {
            "agg": str(rec.get("aggregateMode") or ""),
            "op": DBUFF_OPERATIONS[op_name],
            "m": rows,
        }
    return out


# --------------------------------------------------------------------------
# stage 3: assembly
# --------------------------------------------------------------------------

def referenced_attributes(effects: dict, dbuffs: dict) -> set:
    """Every attribute id a KEPT modifier row or dbuff row names.

    Dropped rows contribute nothing, which is what makes "every attribute a
    modifier references is present in ``attrs``" a total, testable invariant."""
    out = set()
    for rec in effects.values():
        for row in rec["m"]:
            out.add(row[2])
            out.add(row[3])
    for rec in dbuffs.values():
        for row in rec["m"]:
            out.add(row[1])
    return out


# --------------------------------------------------------------------------
# stage 3b: skill-effect overrides (tools/dogma_overrides.py)
# --------------------------------------------------------------------------

def skill_carriers(types: dict, effect_id: int) -> list:
    """The category-16 type ids (as the table's string keys, sorted by id) that
    list ``effect_id``."""
    return [tid for tid in sorted(types, key=int)
            if types[tid].get("c") == SKILL_CATEGORY_ID
            and effect_id in (types[tid].get("e") or ())]


def apply_overrides(effects: dict, types: dict) -> dict:
    """Splice ``dogma_overrides.SKILL_EFFECT_OVERRIDES`` into a built table.

    Mutates ``effects`` and ``types`` in place and returns
    ``{"applied": n, "synthetic": m}`` -- ``applied`` counts effect RECORDS that
    now carry override rows, of which ``synthetic`` are the per-skill ones.

    An overridden effect keeps its real ``cat`` (the engine's state gating reads
    it, and an override changes what the effect DOES, never when it applies).
    An override for an effect this SDE build does not carry is a no-op, so a
    stale entry cannot fabricate an effect out of nothing.

    Per-skill overrides (a dict value) are emitted as one synthetic effect per
    carrying skill under ``dogma_overrides.synthetic_effect_id`` and swapped
    into that skill's ``e`` list; the original effect id is then dropped if no
    kept type still names it.  The current SDE needs none of this -- see the
    module docstring of ``dogma_overrides``."""
    applied = synthetic = 0
    for effect_id in sorted(dogma_overrides.SKILL_EFFECT_OVERRIDES):
        key = str(effect_id)
        if key not in effects:
            continue
        category = effects[key]["cat"]
        if not dogma_overrides.is_per_skill(effect_id):
            effects[key]["m"] = [list(row)
                                 for row in dogma_overrides.rows_for(effect_id, 0)]
            applied += 1
            continue
        for tid in skill_carriers(types, effect_id):
            rows = dogma_overrides.rows_for(effect_id, int(tid))
            if rows is None:
                continue
            new_id = dogma_overrides.synthetic_effect_id(int(tid))
            effects[str(new_id)] = {"cat": category, "m": [list(row) for row in rows]}
            types[tid]["e"] = [new_id if e == effect_id else e for e in types[tid]["e"]]
            applied += 1
            synthetic += 1
        if not any(effect_id in (rec.get("e") or ()) for rec in types.values()):
            del effects[key]
    return {"applied": applied, "synthetic": synthetic}


def untriaged_skill_effects(effects: dict, types: dict) -> dict:
    """``{effect_id: [carrying skill type id, ...]}`` for every effect on a
    category-16 type that STILL has no modifier rows and is not listed in
    ``dogma_overrides.SKILL_EFFECTS_IGNORED``.

    Call after :func:`apply_overrides`; a non-empty result is a hard build
    failure, because such an effect is a bonus the engine silently drops."""
    out = {}
    for tid, rec in types.items():
        if rec.get("c") != SKILL_CATEGORY_ID:
            continue
        for effect_id in rec.get("e") or ():
            row = effects.get(str(effect_id))
            if row is None or row["m"]:
                continue
            if effect_id in dogma_overrides.SKILL_EFFECTS_IGNORED:
                continue
            out.setdefault(effect_id, []).append(int(tid))
    return {eid: sorted(skills) for eid, skills in sorted(out.items())}


def ignored_skill_effect_count(effects: dict, types: dict) -> int:
    """How many distinct modifier-less skill effects the shipped table carries
    under ``SKILL_EFFECTS_IGNORED`` (the summary's ``K``)."""
    seen = set()
    for rec in types.values():
        if rec.get("c") != SKILL_CATEGORY_ID:
            continue
        for effect_id in rec.get("e") or ():
            row = effects.get(str(effect_id))
            if row is not None and not row["m"]:
                seen.add(effect_id)
    return len(seen)


def build_table(*, groups_lines, types_lines, type_dogma_lines, effects_lines,
                attributes_lines, dbuff_lines, fit_type_ids, build: int,
                stats: dict | None = None) -> dict:
    """Assemble the whole table from six JSONL line iterables.

    Each iterable is consumed exactly once, in this order, so the caller can hand
    over streaming zip members (production) or plain lists (tests).

    ``stats``, when given, is populated with generation-run metrics that do NOT
    belong in the wire table itself -- currently just ``unknown_fit_type_ids``
    (see :func:`unknown_fit_type_ids`), which :func:`emit` reports in its
    summary. ``fit_type_ids`` must therefore be re-iterable (a list, not a
    one-shot generator); every caller already passes one."""
    group_categories = load_group_categories(groups_lines)
    index = index_types(types_lines, group_categories)
    if stats is not None:
        stats["unknown_fit_type_ids"] = unknown_fit_type_ids(index, fit_type_ids)
    kept_types = select_types(index, fit_type_ids)
    type_dogma = load_type_dogma(type_dogma_lines, kept_types)

    kept_effect_ids = set()
    for _attrs, effect_ids in type_dogma.values():
        kept_effect_ids.update(effect_ids)
    effects = load_effects(effects_lines, kept_effect_ids)

    attributes = load_attributes(attributes_lines)
    dbuffs = normalise_dbuffs(dbuff_lines)

    kept_attrs = (referenced_attributes(effects, dbuffs)
                  | resolve_whitelist(attributes)
                  | dogma_overrides.referenced_attribute_ids())
    kept_attrs &= set(attributes)          # an id no SDE record defines cannot be encoded

    out_attrs = {}
    for aid in sorted(kept_attrs):
        _name, default, stackable, high_is_good = attributes[aid]
        out_attrs[str(aid)] = [default, 1 if stackable else 0, 1 if high_is_good else 0]

    out_types = {}
    for tid in sorted(kept_types):
        own_attrs, effect_ids = type_dogma.get(tid, ({}, ()))
        flat = []
        for aid in sorted(own_attrs):
            if aid not in kept_attrs:
                continue
            value = own_attrs[aid]
            if value == attributes[aid][1]:    # equals the attribute default: implied
                continue
            flat.append(aid)
            flat.append(value)
        rec = {"g": index.group[tid], "c": index.category[tid]}
        if flat:
            rec["a"] = flat
        kept_ids = [e for e in effect_ids if str(e) in effects]
        if kept_ids:
            rec["e"] = kept_ids
        out_types[str(tid)] = rec

    override_stats = apply_overrides(effects, out_types)
    untriaged = untriaged_skill_effects(effects, out_types)
    if untriaged:
        # Exit 2 (not a bare FATAL/1) so a CI caller can tell "this SDE grew a
        # new modifier-less skill effect" apart from a broken invocation.
        print("UNTRIAGED SKILL EFFECT(S): in NEITHER "
              "dogma_overrides.SKILL_EFFECT_OVERRIDES nor SKILL_EFFECTS_IGNORED --",
              file=sys.stderr)
        for eid, skills in untriaged.items():
            print(f"  effect {eid} on skill(s) "
                  f"{', '.join(str(s) for s in skills)}", file=sys.stderr)
        print("Triage each one (write an override row, or list it as ignored with a "
              "reason) before regenerating; shipping it would silently drop the bonus.",
              file=sys.stderr)
        raise SystemExit(2)
    if stats is not None:
        stats["overrides"] = override_stats
        stats["ignored_skill_effects"] = ignored_skill_effect_count(effects, out_types)

    return {
        "v": int(build),
        "attrs": out_attrs,
        "types": out_types,
        "effects": effects,
        "dbuffs": dbuffs,
        "skills": sorted(index.skills & kept_types),
    }


def encode(table: dict) -> tuple[bytes, bytes]:
    """``(json text bytes, gzip level-9 bytes)`` -- the two budgeted sizes.

    ``mtime=0`` keeps the gzip header (and therefore the whole artifact) BYTE
    REPRODUCIBLE: regenerating from the same SDE build produces an identical
    file, so ``git status`` stays quiet unless the DATA actually changed."""
    text = json.dumps(table, separators=(",", ":")).encode("utf-8")
    return text, gzip.compress(text, 9, mtime=0)


def check_budgets(text_bytes: int, gzip_bytes: int) -> list:
    """The budget breaches, as human-readable strings (empty == within budget).

    Reads the budgets through the module globals so a test can shrink them."""
    problems = []
    if gzip_bytes > GZIP_BUDGET_BYTES:
        problems.append(f"gzip {gzip_bytes:,} B exceeds budget {GZIP_BUDGET_BYTES:,} B")
    if text_bytes > TEXT_BUDGET_BYTES:
        problems.append(f"decoded JSON text {text_bytes:,} B exceeds budget "
                        f"{TEXT_BUDGET_BYTES:,} B")
    return problems


def atomic_write(path: Path, payload: bytes) -> None:
    """temp + fsync + ``os.replace`` (the ``gen_fit_types``/``app_io`` pattern), so
    a crash mid-write can never leave a truncated tracked artifact behind."""
    tmp = Path(f"{path}.tmp")
    with open(tmp, "wb") as fh:
        fh.write(payload)
        fh.flush()
        try:
            os.fsync(fh.fileno())
        except (OSError, AttributeError):
            pass
    os.replace(tmp, path)


# --------------------------------------------------------------------------
# zip / CLI
# --------------------------------------------------------------------------

def read_build(zf: zipfile.ZipFile) -> int | None:
    """The SDE build number out of the zip's own ``_sde.jsonl``, or None."""
    try:
        raw = zf.read(MEMBER_BUILD)
    except KeyError:
        return None
    for rec in iter_jsonl(raw.splitlines()):
        number = rec.get("buildNumber")
        if isinstance(number, int):
            return number
    return None


def _download(force: bool) -> tuple[Path, int | None]:
    import requests  # repo dependency; imported here so tests need no network stack
    CACHE_DIR.mkdir(exist_ok=True)
    zpath = CACHE_DIR / "sde.zip"
    if force or not zpath.exists():
        print(f"downloading {SDE_ZIP_URL} ...")
        resp = requests.get(SDE_ZIP_URL, timeout=600)
        resp.raise_for_status()
        zpath.write_bytes(resp.content)
    build = None
    try:
        meta = requests.get(SDE_BUILD_URL, timeout=30)
        if meta.ok and meta.text.strip():
            build = int(json.loads(meta.text.strip().splitlines()[0])["buildNumber"])
    except Exception as exc:   # the zip's own _sde.jsonl is the primary source
        print(f"warn: build lookup failed: {exc}")
    return zpath, build


def _resident_mb(payload: bytes) -> float:
    """Peak MB of the live decoded object graph -- REPORTED, never asserted
    (tracemalloc is far too noisy to gate on under xdist)."""
    tracemalloc.start()
    obj = json.loads(payload.decode("utf-8"))
    _current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    del obj
    return peak / (1024 * 1024)


def main(argv: list | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--sde-zip", type=Path, help="path to the SDE jsonl zip")
    src.add_argument("--download", action="store_true", help="fetch the latest SDE zip (cached)")
    ap.add_argument("--out", type=Path, default=REPO_ROOT / "fit_dogma.json.gz")
    ap.add_argument("--fit-types", type=Path, default=REPO_ROOT / "fit_types.json")
    ap.add_argument("--build", type=int, default=None, help="override the SDE build stamp")
    ap.add_argument("--force", action="store_true", help="re-download even if cached")
    args = ap.parse_args(argv)

    zpath, downloaded_build = args.sde_zip, None
    if args.download:
        zpath, downloaded_build = _download(args.force)

    fit_type_ids = [int(t) for t in json.loads(args.fit_types.read_text(encoding="utf-8"))]

    try:
        zf = zipfile.ZipFile(zpath)
    except zipfile.BadZipFile as exc:
        raise SystemExit(f"FATAL: {zpath} is not a valid zip file: {exc}")
    with zf:
        missing = [m for m in (MEMBER_GROUPS, MEMBER_TYPES, MEMBER_TYPE_DOGMA,
                               MEMBER_EFFECTS, MEMBER_ATTRIBUTES, MEMBER_DBUFFS)
                   if m not in zf.namelist()]
        if missing:
            raise SystemExit(f"FATAL: {zpath} is missing member(s): {', '.join(missing)}")
        build = args.build or read_build(zf) or downloaded_build
        if not build:
            raise SystemExit(
                f"FATAL: no SDE build number ({MEMBER_BUILD} absent from {zpath} and no "
                "--build given) -- the table stamps it and dogma_data requires an int")
        stats = {}
        # Streamed one member at a time: types.jsonl alone is 152 MB uncompressed.
        with zf.open(MEMBER_GROUPS) as groups, zf.open(MEMBER_TYPES) as types, \
                zf.open(MEMBER_TYPE_DOGMA) as tdog, zf.open(MEMBER_EFFECTS) as effects, \
                zf.open(MEMBER_ATTRIBUTES) as attrs, zf.open(MEMBER_DBUFFS) as dbuffs:
            table = build_table(groups_lines=groups, types_lines=types,
                                type_dogma_lines=tdog, effects_lines=effects,
                                attributes_lines=attrs, dbuff_lines=dbuffs,
                                fit_type_ids=fit_type_ids, build=build, stats=stats)

    return emit(table, args.out, stats=stats)


def emit(table: dict, out_path: Path, *, stats: dict | None = None) -> int:
    """Encode, write and report one table; ``SystemExit(2)`` on a budget breach.

    The file is written BEFORE the gate so an over-budget table can be inspected
    rather than merely described."""
    text, blob = encode(table)
    atomic_write(out_path, blob)

    pairs = sum(len(rec.get("a", ())) // 2 for rec in table["types"].values())
    mods = sum(len(rec["m"]) for rec in table["effects"].values())
    unknown = tuple((stats or {}).get("unknown_fit_type_ids") or ())
    overrides = (stats or {}).get("overrides") or {"applied": 0, "synthetic": 0}
    ignored = (stats or {}).get("ignored_skill_effects", 0)
    print(f"wrote {out_path}")
    print(f"  SDE build      {table['v']}")
    print(f"  types          {len(table['types']):,} ({pairs:,} attribute pairs, "
          f"{len(table['skills']):,} skills)")
    print(f"  effects        {len(table['effects']):,} ({mods:,} modifier rows)")
    print(f"  attributes     {len(table['attrs']):,}")
    print(f"  dbuffs         {len(table['dbuffs']):,}")
    print(f"  skill-effect overrides applied: {overrides['applied']} "
          f"({overrides['synthetic']} synthetic per-skill effects)")
    print(f"  skill effects still without modifiers: {ignored} "
          "(all listed in SKILL_EFFECTS_IGNORED)")
    print(f"  fit_types ids unknown to SDE: {len(unknown)}")
    if unknown:
        shown = ", ".join(str(t) for t in unknown[:10])
        more = f" ... (+{len(unknown) - 10} more)" if len(unknown) > 10 else ""
        print(f"    {shown}{more}")
    print(f"  gzip on disk   {len(blob):,} B (budget {GZIP_BUDGET_BYTES:,})")
    print(f"  decoded text   {len(text):,} B (budget {TEXT_BUDGET_BYTES:,})")
    print(f"  resident peak  {_resident_mb(text):.2f} MB (reported, not budgeted)")

    problems = check_budgets(len(text), len(blob))
    if problems:
        for problem in problems:
            print(f"BUDGET BREACH: {problem}", file=sys.stderr)
        raise SystemExit(2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
