"""Curated modifier rows for SKILL effects the SDE publishes WITHOUT ``modifierInfo``.

Why this file exists
--------------------
A handful of skill effects carry their semantics only in the game client's
compiled code -- CCP never published a ``modifierInfo`` for them.  The SDE row
is real (the effect exists, it is attached to the skill, it has a category) but
its modifier list is empty, so a pure ``modifierInfo`` engine applies NOTHING
and every number those skills feed comes out low.  Measured on the shipped
table: missile DPS at All-V ran ~27 % under the in-game value, because the four
missile-damage skill effects (660/661/662/668) are exactly this shape.

pyfa/EOS solve the same problem by hand-writing a Python handler per effect id.
This module is the same idea in data: the rows below are hand-authored from each
skill's own in-game description line, and the generator splices them into the
table so the ENGINE stays a generic ``modifierInfo`` interpreter with no
per-effect special cases.

Census (SDE build 3494416): **7** effects carried by a category-16 type have an
empty modifier list, across 23 skills.  Six are overridden here; one
(848) touches nothing in the v1 stat set and is listed in
:data:`SKILL_EFFECTS_IGNORED`.  Nothing is silently unlisted -- the generator
exits 2 if a future SDE build introduces a modifier-less skill effect that is in
neither table, so a new gap gets triaged instead of shipped.

The ``-1`` sentinel
-------------------
Every row here bonuses "the things that require THIS skill", so the row's
``skillTypeID`` must be the id of whichever skill carries the effect -- one row,
seven carrying skills.  :data:`SELF_SKILL` (``-1``) is that sentinel: the engine
resolves it to the effect's own source item type.  The real SDE has **zero**
``-1`` skillTypeIDs (Appendix A.3 census: "always a valid positive type id"), so
the sentinel is unambiguous.

Row shape
---------
Identical to what ``gen_fit_dogma.normalise_modifiers`` emits, so the table's
wire format needs no new field and ``dogma_data._modifier`` reads an overridden
row exactly like a native one::

    [domain, func, modifiedAttributeID, modifyingAttributeID, operation,
     skillTypeID | None, groupID | None]

Per-skill divergence
--------------------
:data:`SKILL_EFFECT_OVERRIDES` values are usually a flat tuple of rows (the
effect means the same thing on every skill that carries it).  When a future
effect's semantics DIFFER per carrying skill, the value may instead be a dict
``{skill_type_id: rows, "*": default_rows}``; the generator then emits, per
(effect, skill) pair, a SYNTHETIC effect id ``OVERRIDE_EFFECT_BASE +
skill_type_id`` attached only to that one skill.  **No effect in the current SDE
needs this** -- 1730's nine drone skills differ only in the VALUE of their
``damageMultiplierBonus`` attribute (5 % vs 2 % per level), which is data the
uniform row already reads off each skill.  The mechanism exists so the next
divergence is a table edit rather than a format change.
"""
from __future__ import annotations

# --------------------------------------------------------------------------
# sentinels / ids
# --------------------------------------------------------------------------

#: ``skillTypeID`` meaning "the type of the skill this effect is attached to".
#: The engine resolves it per source item; the SDE never emits -1 itself.
SELF_SKILL = -1

#: Synthetic per-skill effect ids are ``OVERRIDE_EFFECT_BASE + skill_type_id``.
#: Far above every real SDE effect id (the current build's largest is ~13k) and
#: above every type id (~90k), so a synthetic id can never collide with a real
#: effect, and the id itself names the skill it was minted for.
OVERRIDE_EFFECT_BASE = 9_000_000

# --------------------------------------------------------------------------
# attribute ids used below (names are the SDE's own)
# --------------------------------------------------------------------------
ATTR_EM_DAMAGE = 114
ATTR_EXPLOSIVE_DAMAGE = 116
ATTR_KINETIC_DAMAGE = 117
ATTR_THERMAL_DAMAGE = 118
ATTR_DAMAGE_MULTIPLIER = 64
ATTR_SPEED = 51                     # rate of fire, turrets AND launchers (A.7)
ATTR_DAMAGE_MULTIPLIER_BONUS = 292  # per-level bonus, preMul'd by skillLevel
ATTR_ROF_BONUS = 293                # per-level bonus (negative), likewise

#: dogma operation 6 = postPercent -- "+N %", the op every bonus row here uses.
OP_POST_PERCENT = 6

# --------------------------------------------------------------------------
# the overrides
# --------------------------------------------------------------------------

#: The two-step skill pattern (spec A.7) is already in the SDE for every skill
#: below: a ``domain: itemID`` ItemModifier preMuls the per-level bonus
#: attribute by ``skillLevel`` (280) -- effect 152 for the missile skills, 146
#: for the drone skills, 163 for the specializations -- so at All V attribute
#: 292/293 already reads 25.0 / -10.0 by the time these rows apply it.  The rows
#: below are ONLY the second step, and must never scale by level themselves.
SKILL_EFFECT_OVERRIDES: dict = {

    # ---- missile size skills: 3320 Rockets, 3321 Light Missiles,
    # 3322 Auto-Targeting Missiles, 3324 Heavy Missiles, 3325 Torpedoes,
    # 3326 Cruise Missiles, 25719 Heavy Assault Missiles.  All seven carry
    # damageMultiplierBonus (292) = 5.0 and all four effects below.
    # Description (Heavy Missiles 3324, verbatim): "Skill with heavy missiles.
    # Special: 5% bonus to heavy missile damage per skill level."
    # The bonus lands on the CHARGE (missile damage lives on the charge, not the
    # launcher), and the charge is character-OWNED -- hence charID +
    # OwnerRequiredSkillModifier, the same shape the SDE itself uses for
    # effect 12802 (Mutated Drone Specialization mining yield).
    # One effect per damage type; the effect NAME is the mapping.
    660: (   # "missileEMDmgBonus"
        ["charID", "OwnerRequiredSkillModifier",
         ATTR_EM_DAMAGE, ATTR_DAMAGE_MULTIPLIER_BONUS, OP_POST_PERCENT, SELF_SKILL, None],
    ),
    661: (   # "missileExplosiveDmgBonus"
        ["charID", "OwnerRequiredSkillModifier",
         ATTR_EXPLOSIVE_DAMAGE, ATTR_DAMAGE_MULTIPLIER_BONUS, OP_POST_PERCENT, SELF_SKILL, None],
    ),
    662: (   # "missileThermalDmgBonus"
        ["charID", "OwnerRequiredSkillModifier",
         ATTR_THERMAL_DAMAGE, ATTR_DAMAGE_MULTIPLIER_BONUS, OP_POST_PERCENT, SELF_SKILL, None],
    ),
    668: (   # "missileKineticDmgBonus2"
        ["charID", "OwnerRequiredSkillModifier",
         ATTR_KINETIC_DAMAGE, ATTR_DAMAGE_MULTIPLIER_BONUS, OP_POST_PERCENT, SELF_SKILL, None],
    ),

    # ---- 1730 "droneDmgBonus" -- 9 skills: 3441 Heavy Drone Operation,
    # 24241 Light Drone Operation, 33699 Medium Drone Operation,
    # 23594 Sentry Drone Interfacing (all damageMultiplierBonus 5.0), and
    # 12484/12485/12486/12487 Amarr/Minmatar/Gallente/Caldari Drone
    # Specialization + 60515 Mutated Drone Specialization (all 2.0).
    # Description (Medium Drone Operation 33699, verbatim): "Skill at
    # controlling medium combat drones. 5% bonus to damage of medium drones per
    # level."  And (Gallente Drone Specialization 12486): "2% bonus per skill
    # level to the damage of light, medium, heavy and sentry drones requiring
    # Gallente Drone Specialization."
    # Both sentences are the SAME rule -- "drones requiring this skill, +N %
    # damage" -- so one uniform row serves all nine; the 5-vs-2 split is the
    # skills' own attribute 292, which the row reads.  Drone damage scales
    # through damageMultiplier (64), never the per-type damage attrs (pyfa's
    # dronedmgbonus does the same); drones are character-OWNED items.
    # 60515's mining-yield clause is a DIFFERENT effect (12802) and already has
    # a real modifierInfo -- nothing here touches it.
    1730: (
        ["charID", "OwnerRequiredSkillModifier",
         ATTR_DAMAGE_MULTIPLIER, ATTR_DAMAGE_MULTIPLIER_BONUS, OP_POST_PERCENT, SELF_SKILL, None],
    ),

    # ---- 1851 "selfRof" -- 6 missile specialization skills: 20209 Rocket,
    # 20210 Light Missile, 20211 Heavy Missile, 20212 Cruise Missile,
    # 20213 Torpedo, 25718 Heavy Assault Missile Specialization.  All six carry
    # rofBonus (293) = -2.0 (NOT damageMultiplierBonus -- this is rate of fire,
    # not damage).
    # Description (Heavy Missile Specialization 20211, verbatim): "Specialist
    # training in the operation of advanced heavy missile launchers. 2% bonus
    # per level to the rate of fire of modules requiring Heavy Missile
    # Specialization."
    # "modules requiring" => the LAUNCHER, which is ship-located: shipID +
    # LocationRequiredSkillModifier, the same shape as Gunnery's effect 414
    # (spec A.7).  Rate of fire is attribute 51 ``speed`` (A.7: there is no
    # rateOfFire attribute); the bonus is negative, so a faster cycle.
    1851: (
        ["shipID", "LocationRequiredSkillModifier",
         ATTR_SPEED, ATTR_ROF_BONUS, OP_POST_PERCENT, SELF_SKILL, None],
    ),
}


#: Modifier-less skill effects deliberately NOT overridden, with the reason.
#: An effect must be here or in :data:`SKILL_EFFECT_OVERRIDES`; the generator
#: fails loud on anything in neither, so a new SDE gap cannot ship untriaged.
SKILL_EFFECTS_IGNORED: dict = {
    # 848 "cloakingTargetingDelayBonus...ForShipModulesRequiringCloaking",
    # skill 11579 Cloaking (cloakingTargetingDelayBonus 619 = -10.0):
    # "10% reduction in targeting delay after uncloaking per skill level."
    # It would modify cloakingTargetingDelay (560) -- not DPS, volley, range,
    # EHP or any other v1 stat.
    848: "cloaking re-target delay (attr 560) -- not in the v1 stat set",
}


# --------------------------------------------------------------------------
# accessors
# --------------------------------------------------------------------------

def synthetic_effect_id(skill_type_id: int) -> int:
    """The reserved effect id a per-skill override is emitted under."""
    return OVERRIDE_EFFECT_BASE + int(skill_type_id)


def is_per_skill(effect_id: int) -> bool:
    """True when this effect's override differs per carrying skill and must
    therefore be emitted as synthetic per-skill effects."""
    return isinstance(SKILL_EFFECT_OVERRIDES.get(effect_id), dict)


def rows_for(effect_id: int, skill_type_id: int) -> tuple | None:
    """The override rows for one (effect, carrying skill) pair.

    ``None`` means "no override applies": either the effect is not overridden at
    all, or its per-skill table names neither this skill nor a ``"*"`` default
    (an explicit way to say "this one skill gets nothing")."""
    spec = SKILL_EFFECT_OVERRIDES.get(effect_id)
    if spec is None:
        return None
    if isinstance(spec, dict):
        if skill_type_id in spec:
            return spec[skill_type_id]
        return spec.get("*")
    return spec


def referenced_attribute_ids() -> frozenset:
    """Every attribute id any override row names.

    The generator unions this into its kept-attribute set, so "every attribute a
    kept modifier references is present in ``attrs``" stays a total invariant
    even for attributes no NATIVE modifier happens to reference."""
    out = set()
    for spec in SKILL_EFFECT_OVERRIDES.values():
        groups = spec.values() if isinstance(spec, dict) else (spec,)
        for rows in groups:
            for row in rows:
                out.add(int(row[2]))
                out.add(int(row[3]))
    return frozenset(out)


# --------------------------------------------------------------------------
# self-check (import time -- a malformed table must never reach a build)
# --------------------------------------------------------------------------

_VALID_OPERATIONS = frozenset({-1, 0, 2, 3, 4, 5, 6, 7})


def _check_row(effect_id, row) -> None:
    if not isinstance(row, list) or len(row) != 7:
        raise ValueError(f"override row for effect {effect_id} must be a 7-column "
                         f"list, got {row!r}")
    domain, func, modified, modifying, op, skill_id, group_id = row
    if not isinstance(domain, str) or not isinstance(func, str):
        raise ValueError(f"override row for effect {effect_id}: domain/func must be str")
    if not isinstance(modified, int) or not isinstance(modifying, int):
        raise ValueError(f"override row for effect {effect_id}: attribute ids must be int")
    if op not in _VALID_OPERATIONS:
        raise ValueError(f"override row for effect {effect_id}: operation {op!r} is not "
                         f"one of {sorted(_VALID_OPERATIONS)}")
    if skill_id is not None and not isinstance(skill_id, int):
        raise ValueError(f"override row for effect {effect_id}: skillTypeID must be int/None")
    if group_id is not None and not isinstance(group_id, int):
        raise ValueError(f"override row for effect {effect_id}: groupID must be int/None")


def _validate() -> None:
    overlap = set(SKILL_EFFECT_OVERRIDES) & set(SKILL_EFFECTS_IGNORED)
    if overlap:
        raise ValueError(f"effect(s) {sorted(overlap)} are in BOTH SKILL_EFFECT_OVERRIDES "
                         "and SKILL_EFFECTS_IGNORED -- an effect is one or the other")
    for effect_id, spec in SKILL_EFFECT_OVERRIDES.items():
        if effect_id >= OVERRIDE_EFFECT_BASE:
            raise ValueError(f"effect {effect_id} collides with the synthetic id range "
                             f"(>= {OVERRIDE_EFFECT_BASE})")
        groups = spec.values() if isinstance(spec, dict) else (spec,)
        for rows in groups:
            if rows is None:
                continue
            for row in rows:
                _check_row(effect_id, row)
    for effect_id, reason in SKILL_EFFECTS_IGNORED.items():
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError(f"SKILL_EFFECTS_IGNORED[{effect_id}] needs a non-empty reason")


_validate()
