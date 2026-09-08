"""Turn an evaluated fit into DPS / volley / range / EHP -- the fit-sim facade.

Pure stdlib, no Tk, no network, no ``fc_gui``.  Layering (imports flow only
downward)::

    dogma_data <- fit_sim <- fit_sim_links <- fit_sim_stats <- {panel, fleet}

:mod:`fit_sim` knows nothing about weapons or resists: it hands back items whose
``attrs`` carry fully modified numbers.  THIS module is the one that decides what
those numbers mean, which is why the **effect-handler registry lives here** (spec
Appendix A.8): the engine records every effect it could not model, and this layer
decides which of those the USER is told about.

Public surface:

* :func:`derive` -- an evaluated :class:`fit_sim.FitState` -> :class:`FitStats`.
* :func:`simulate` -- a :class:`fit_models.ParsedFit` -> :class:`FitStats`,
  through a bounded LRU.  The single entry point every consumer uses.
* :func:`clear_cache`.

**Nothing loads at startup.**  :func:`simulate` raises
:class:`dogma_data.DogmaUnavailable` when no table is installed; it never calls
``dogma_data.load()`` itself, because that is a multi-megabyte decode and the
caller's worker thread owns the decision to pay for it.

v1 simplifications, each of them honest -- every one shows up in
``FitStats.unmodeled`` and (where it moves a number) sets ``FitStats.partial``:

* only the four dbuffs that move a v1 stat are visible in the numbers -- a
  Skirmish or Information link changes nothing the readout shows, so those
  disciplines have no preset at all (:mod:`fit_sim_links`);
* the Reactive Armor Hardener's resist shift is not simulated;
* smartbomb damage is excluded from DPS;
* missile application (explosion radius / velocity vs a target), capacitor,
  overheating, T3 subsystems and fighters are out of scope for v1.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import Callable, NamedTuple, Sequence

import dogma_data
import fit_sim
import fit_sim_links
from fit_models import ParsedFit, fit_content_hash

# ===========================================================================
# attribute ids
# ===========================================================================
#: Every attribute this module reads, BY NAME, verified against the SDE build
#: the bundled table was generated from (``dogmaAttributes.jsonl``: name -> id,
#: default, stackable, highIsGood).  Nothing here is remembered or guessed --
#: ``tools/gen_fit_dogma.py``'s whitelist names the same attributes, and the
#: golden tests read the real table, so a renamed or renumbered attribute is a
#: test failure rather than a silently wrong number.
ATTR = {
    # hit points (the three tank layers)
    "hp": 9,                                # hull HP
    "shieldCapacity": 263,
    "armorHP": 265,
    # resonances = 1 - resist.  SDE order within a layer is em / explosive /
    # kinetic / thermal -- NEVER assume it matches DamageProfile's field order,
    # which is em / thermal / kinetic / explosive.  Both orders are spelled out
    # explicitly in LAYER_SPECS below.
    "shieldEmDamageResonance": 271,
    "shieldExplosiveDamageResonance": 272,
    "shieldKineticDamageResonance": 273,
    "shieldThermalDamageResonance": 274,
    "armorEmDamageResonance": 267,
    "armorExplosiveDamageResonance": 268,
    "armorKineticDamageResonance": 269,
    "armorThermalDamageResonance": 270,
    "hullEmDamageResonance": 974,
    "hullExplosiveDamageResonance": 975,
    "hullKineticDamageResonance": 976,
    "hullThermalDamageResonance": 977,
    # damage (on the CHARGE for turrets/missiles, on the DRONE for drones)
    "emDamage": 114,
    "explosiveDamage": 116,
    "kineticDamage": 117,
    "thermalDamage": 118,
    # weapon
    "damageMultiplier": 64,
    "speed": 51,                            # rate of fire, in MILLISECONDS
    "maxRange": 54,                         # turret optimal
    "falloff": 158,
    "maxVelocity": 37,                      # missile flight speed (m/s)
    "explosionDelay": 281,                  # missile flight time, in MS
    # drones
    "droneBandwidth": 1271,                 # on the ship
    "droneBandwidthUsed": 1272,             # on the drone
    "maxActiveDrones": 352,                 # on the ship
}

#: Damage attribute ids in :class:`DamageProfile` field order (em, th, kin, exp).
DAMAGE_ATTRS = (ATTR["emDamage"], ATTR["thermalDamage"],
                ATTR["kineticDamage"], ATTR["explosiveDamage"])

#: ``(layer name, HP attribute, resonance attributes in PROFILE order)``.
#: The resonance tuples are written out em / thermal / kinetic / explosive --
#: the SDE numbers them em / explosive / kinetic / thermal, so a positional copy
#: of the id ranges would silently swap thermal and explosive on every layer.
LAYER_SPECS = (
    ("shield", ATTR["shieldCapacity"],
     (ATTR["shieldEmDamageResonance"], ATTR["shieldThermalDamageResonance"],
      ATTR["shieldKineticDamageResonance"],
      ATTR["shieldExplosiveDamageResonance"])),
    ("armor", ATTR["armorHP"],
     (ATTR["armorEmDamageResonance"], ATTR["armorThermalDamageResonance"],
      ATTR["armorKineticDamageResonance"],
      ATTR["armorExplosiveDamageResonance"])),
    ("hull", ATTR["hp"],
     (ATTR["hullEmDamageResonance"], ATTR["hullThermalDamageResonance"],
      ATTR["hullKineticDamageResonance"],
      ATTR["hullExplosiveDamageResonance"])),
)

# ===========================================================================
# the effect-handler registry (spec A.5 / A.8)
# ===========================================================================
# 46 of the 469 effects on the owner's library carry no ``modifierInfo``. The
# engine reports every one of them; these five constants are how this layer
# tells "harmless" from "we are lying to you". Each has a named test.

KIND_TURRET = "turret"
KIND_MISSILE = "missile"
KIND_DRONE = "drone"

#: Weapon CLASSIFIERS: they carry no modifier in-game either -- they NAME the
#: attributes that hold a weapon's RoF / range / falloff, which is exactly how
#: pyfa/eos classifies weapons too.  A value of ``None`` is an explicit no-op:
#: an effect of the weapon family that fires nothing this layer counts.
WEAPON_EFFECTS = {
    10: KIND_TURRET,        # targetAttack     -- turrets and drones
    34: KIND_TURRET,        # projectileFired
    263: KIND_TURRET,       # barrage          -- long-range ammo (falloff 328)
    101: KIND_MISSILE,      # useMissiles      -- the launcher
    9: KIND_MISSILE,        # missileLaunching -- the missile itself
    103: None,              # defenderMissileLaunching
    127: None,              # torpedoLaunching (a Warp Disrupt Probe)
    3793: None,             # probeLaunching
}

#: The two fitting markers that classify a weapon module even when its
#: classifier effect is absent.  Checked FIRST -- they are the game's own
#: hardpoint bookkeeping and never lie about turret vs launcher.
TURRET_FITTED = 42
LAUNCHER_FITTED = 40

#: Slot markers and activation-only effects: real effects that genuinely change
#: no v1 stat, so reporting them would be pure noise (spec A.5 group b).
NO_OP_EFFECTS = frozenset({
    # slot / fitting markers
    11,      # loPower
    12,      # hiPower
    13,      # medPower
    16,      # online
    2663,    # rigSlot
    TURRET_FITTED,
    LAUNCHER_FITTED,
    3772,    # subSystem
    3773,    # hardPointModifierEffect
    3774,    # slotModifier
    # activation-only / out of the v1 stat set
    4,       # shieldBoosting
    48,      # powerBooster
    3380,    # warpDisruptSphere
    6184,    # shipModuleRemoteCapacitorTransmitter
    6186,    # shipModuleRemoteShieldBooster
    6187,    # energyNeutralizerFalloff
    6188,    # shipModuleRemoteArmorRepairer
    6197,    # energyNosferatuFalloff
    6208,    # microJumpPortalDrive
    6423,    # shipModuleGuidanceDisruptor
    6425,    # remoteTargetPaintFalloff
    6426,    # remoteWebifierFalloff
    6470,    # remoteECMFalloff
    6652,    # shipModuleAncillaryRemoteShieldBooster
    6687,    # npcEntityRemoteArmorRepairer
    6688,    # npcEntityRemoteShieldBooster
    6690,    # remoteWebifierEntity
    6695,    # entityECMFalloff
    6714,    # ECMBurstJammer
    7166,    # ShipModuleRemoteArmorMutadaptiveRepairer
})

#: The command-burst -> gang-buff path.  Their semantics live in
#: ``dbuffCollections``, applied by task 4's link tiers; an unmodelled marker
#: effect on a burst module is not a gap the user needs told about.
GANG_BUFF_EFFECTS = frozenset({
    6732,    # moduleBonusWarfareLinkArmor
    6733,    # moduleBonusWarfareLinkShield
    6734,    # moduleBonusWarfareLinkSkirmish
    6735,    # moduleBonusWarfareLinkInfo
    6736,    # moduleBonusWarfareLinkMining
})

#: The two effects that really DO move a v1 stat and really are not simulated.
#: Reported with their own wording, and each of them sets ``partial``.
SPECIAL_STAT_EFFECTS = {
    4928: "Reactive Armor Hardener (not simulated)",
    38: "smartbomb damage excluded",
}

#: v2 watchlist -- ``moduleBonusMicrowarpdrive`` 6730 and
#: ``moduleBonusAfterburner`` 6731 belong in ``SPECIAL_STAT_EFFECTS`` the moment
#: speed or signature radius enters the stat set (49 of the owner's 63 fits
#: carry an MWD).  They change nothing in the v1 set, so v1 reports them like
#: any other unmodelled effect rather than pretending to know better.

#: Only things the PILOT fitted can be an honest "we could not model this".
#: Skills and the character are the engine's own furniture: every fit carries
#: the whole skill tree, so a skill effect that reached nothing means "this
#: skill does not apply to this hull", not a modelling gap -- reporting those
#: buries the real entries under ~300 lines of noise (measured on the owner's
#: Machariel: 322 unmodeled lines, 2 of them about the fit).
REPORTABLE_CATEGORIES = frozenset({
    fit_sim.CATEGORY_SHIP, fit_sim.CATEGORY_MODULE,
    fit_sim.CATEGORY_CHARGE, fit_sim.CATEGORY_DRONE,
})

#: Cache bound for :func:`simulate` (spec 5.4).
CACHE_SIZE = 512

#: The no-links tier, re-exported so a consumer needs one import.  Single
#: source: :mod:`fit_sim_links` owns the tier vocabulary.
TIER_NONE = fit_sim_links.TIER_NONE


# ===========================================================================
# result types
# ===========================================================================

class DamageProfile(NamedTuple):
    """Incoming damage mix used for EHP.  Fractions, conventionally summing to
    1.0 -- a profile that sums to something else scales EHP accordingly, which
    is what an "all EM" profile of ``(1, 0, 0, 0)`` wants."""
    em: float
    th: float
    kin: float
    exp: float


OMNI = DamageProfile(0.25, 0.25, 0.25, 0.25)


class WeaponRange(NamedTuple):
    """One weapon group's reach.  Missiles and drones with no falloff report
    ``falloff_m = 0.0``; a missile's ``optimal_m`` is its maximum flight range."""
    name: str
    count: int
    kind: str                       # "turret" | "missile" | "drone"
    optimal_m: float
    falloff_m: float


@dataclass(frozen=True)
class FitStats:
    """Everything the readout shows for one fit, at All-V skills."""

    dps_total: float
    dps_turret: float
    dps_missile: float
    dps_drone: float
    volley: float
    ranges: tuple[WeaponRange, ...]
    hp_shield: float
    hp_armor: float
    hp_hull: float
    ehp_shield: float
    ehp_armor: float
    ehp_hull: float
    ehp_total: float
    #: layer -> (em, th, kin, exp) as RESIST fractions, i.e. ``1 - resonance``.
    resists: dict
    links: str
    disciplines_applied: tuple[str, ...]
    skills: str = "all_v"
    unmodeled: tuple[str, ...] = ()
    sde_build: int = 0
    #: True when something the engine could not model would have moved a
    #: number: a weapon, charge or drone, or one of SPECIAL_STAT_EFFECTS.
    partial: bool = False


# ===========================================================================
# naming
# ===========================================================================

def _default_name(type_id: int) -> str:
    """The name a caller that supplied no resolver gets.  Deliberately ugly:
    a readout showing "type 2488" is a missing ``name_of``, not a data gap."""
    return f"type {type_id}"


def _namer(name_of: Callable[[int], str] | None) -> Callable[[int], str]:
    """Wrap ``name_of`` so an unknown id (``type_catalog.resolve_name`` answers
    ``None``) or a raising resolver still yields a printable name."""
    if name_of is None:
        return _default_name

    def resolve(type_id: int) -> str:
        try:
            name = name_of(type_id)
        except Exception:
            return _default_name(type_id)
        return name if isinstance(name, str) and name else _default_name(type_id)

    return resolve


# ===========================================================================
# weapons
# ===========================================================================

def _weapon_kind(item) -> str | None:
    """"turret" / "missile" / None for one fitted module.

    The hardpoint markers win: ``turretFitted`` / ``launcherFitted`` are the
    game's own bookkeeping.  Only then do the classifier effects speak, so a
    launcher that also carries ``barrage``-flavoured ammo cannot be misread.
    An explicit ``None`` in :data:`WEAPON_EFFECTS` (a defender missile, a probe
    launcher) is not a weapon this layer counts.
    """
    effects = item.effects
    if TURRET_FITTED in effects:
        return KIND_TURRET
    if LAUNCHER_FITTED in effects:
        return KIND_MISSILE
    for effect_id in effects:
        kind = WEAPON_EFFECTS.get(effect_id)
        if kind:
            return kind
    return None


def _damage_sum(item) -> float:
    """The four damage types of one charge or drone, added.

    Read through :func:`fit_sim.attr` so an absent attribute answers with its
    dogma DEFAULT rather than a bare zero -- the table stores only values that
    differ from the default, so ``attrs.get`` is wrong by construction."""
    return sum(fit_sim.attr(item, attr_id) for attr_id in DAMAGE_ATTRS)


def _cycle_seconds(item) -> float:
    """Rate of fire in seconds.  Attribute 51 ``speed`` is milliseconds for
    turrets, launchers AND drones (spec A.7: there is no ``rateOfFire``)."""
    return fit_sim.attr(item, ATTR["speed"]) / 1000.0


def _weapon_stats(fit, name):
    """Per-weapon DPS / volley / ranges for the fitted modules.

    Returns ``(dps_turret, dps_missile, volley, range_rows, missing_charge)``.
    An offline module contributes nothing (the engine already gives it no
    effects; DPS follows the same rule).  A turret or launcher with NO charge
    contributes zero and is reported -- that is a real hole in the number, so
    it also sets ``partial``.
    """
    dps_turret = 0.0
    dps_missile = 0.0
    volley_total = 0.0
    rows = []
    missing_charge = []

    for module in fit.modules:
        if module.state == fit_sim.STATE_OFFLINE:
            continue
        kind = _weapon_kind(module)
        if kind is None:
            continue
        module_name = name(module.type_id)
        charge = module.charge
        if charge is None:
            missing_charge.append(f"{module_name}: no charge loaded")
            continue

        if kind == KIND_TURRET:
            # Turret volley = the charge's damage times the GUN's multiplier;
            # the engine has already folded skills, ship bonuses and the
            # charge's own otherID modifiers into both numbers.
            volley = _damage_sum(charge) * fit_sim.attr(
                module, ATTR["damageMultiplier"])
            optimal = fit_sim.attr(module, ATTR["maxRange"])
            falloff = fit_sim.attr(module, ATTR["falloff"])
        else:
            # Missile damage lives entirely on the charge: the engine applied
            # the missile skills to it through OwnerRequiredSkillModifier, so
            # multiplying by the launcher would double-count.
            volley = _damage_sum(charge)
            optimal = (fit_sim.attr(charge, ATTR["maxVelocity"])
                       * fit_sim.attr(charge, ATTR["explosionDelay"]) / 1000.0)
            falloff = 0.0

        cycle = _cycle_seconds(module)
        dps = volley / cycle if cycle > 0 else 0.0
        if kind == KIND_TURRET:
            dps_turret += dps
        else:
            dps_missile += dps
        volley_total += volley
        rows.append((module_name, kind, optimal, falloff, 1))

    return dps_turret, dps_missile, volley_total, rows, missing_charge


def _max_active_drones(fit) -> int:
    """How many drones can be in space at once.

    MEASURED, and the reason this is not a one-liner on the ship: hulls do NOT
    carry ``maxActiveDrones``.  The Drones skill does -- its effect 316 is a
    ``charID`` ``ItemModifier`` that modAdds attribute 352 on the CHARACTER (5
    at All V).  Reading it off the ship, as a naive port of the rule would,
    yields 0 and silently zeroes every drone's DPS.  The ship is still consulted
    as a fallback for the hulls (and NPC types) that do declare it.
    """
    if fit.character is not None:
        from_character = fit_sim.attr(fit.character, ATTR["maxActiveDrones"])
        if from_character > 0:
            return int(from_character)
    return int(fit_sim.attr(fit.ship, ATTR["maxActiveDrones"]))


def _drone_stats(fit, name):
    """Drone DPS / volley / ranges, honouring the launch limits.

    Active count per stack = ``min(quantity, remaining drone slots,
    floor(remaining bandwidth / bandwidth used))``, taken GREEDILY in fit order:
    the first stack listed gets its drones out first, which is what a pilot
    does and what makes the number reproducible.
    """
    remaining_slots = _max_active_drones(fit)
    remaining_bandwidth = fit_sim.attr(fit.ship, ATTR["droneBandwidth"])
    dps_total = 0.0
    volley_total = 0.0
    rows = []

    for drone in fit.drones:
        if remaining_slots <= 0:
            break
        active = min(drone.quantity, remaining_slots)
        used = fit_sim.attr(drone, ATTR["droneBandwidthUsed"])
        if used > 0:
            active = min(active, int(remaining_bandwidth // used))
        if active <= 0:
            continue
        remaining_slots -= active
        remaining_bandwidth -= active * used

        volley = _damage_sum(drone) * fit_sim.attr(drone,
                                                   ATTR["damageMultiplier"])
        cycle = _cycle_seconds(drone)
        dps_total += (volley / cycle * active) if cycle > 0 else 0.0
        volley_total += volley * active
        rows.append((name(drone.type_id), KIND_DRONE,
                     fit_sim.attr(drone, ATTR["maxRange"]),
                     fit_sim.attr(drone, ATTR["falloff"]), active))

    return dps_total, volley_total, rows


def _weapon_ranges(rows) -> tuple[WeaponRange, ...]:
    """Fold identical weapon rows into counted groups, first-seen order."""
    grouped: OrderedDict = OrderedDict()
    for name, kind, optimal, falloff, count in rows:
        key = (name, kind, round(optimal, 3), round(falloff, 3))
        grouped[key] = grouped.get(key, 0) + count
    return tuple(WeaponRange(name=key[0], count=count, kind=key[1],
                             optimal_m=key[2], falloff_m=key[3])
                 for key, count in grouped.items())


# ===========================================================================
# tank
# ===========================================================================

def _layer_stats(ship, profile: DamageProfile):
    """``(hp, ehp, resists)`` per tank layer, keyed by layer name.

    ``EHP = HP / sum(profile_i * resonance_i)``: the weighted average resonance
    is what the incoming mix actually meets.  Resonances are read through
    :func:`fit_sim.attr` so an unmodified layer answers with the dogma default
    of 1.0 (= 0 % resist) instead of a 0.0 that would divide by zero and report
    an infinite tank.
    """
    out = {}
    for layer, hp_attr, resonance_attrs in LAYER_SPECS:
        hp = fit_sim.attr(ship, hp_attr)
        resonances = tuple(fit_sim.attr(ship, a) for a in resonance_attrs)
        weighted = sum(p * r for p, r in zip(profile, resonances))
        ehp = hp / weighted if weighted > 0 else 0.0
        out[layer] = (hp, ehp, tuple(1.0 - r for r in resonances))
    return out


# ===========================================================================
# unmodeled reporting
# ===========================================================================

def _items_by_type(fit) -> dict:
    """First item per type id -- what an ``("effect", type, effect)`` key needs
    to answer "is this thing a weapon?"."""
    found: dict = {}
    for item in fit.all_items():
        found.setdefault(item.type_id, item)
    return found


def _is_weapon_like(item) -> bool:
    """Whether an unmodelled effect on this item makes the numbers wrong: a
    weapon module, or any charge or drone (their attributes ARE the damage)."""
    if item is None:
        return False
    if item.category_id in (fit_sim.CATEGORY_CHARGE, fit_sim.CATEGORY_DRONE):
        return True
    return _weapon_kind(item) is not None


def _unmodeled_entries(fit, name):
    """``(lines, partial)`` for everything the engine could not model.

    ``FitState.unmodeled_keys`` is the STRUCTURED record (the ``unmodeled``
    list is the same information already rendered for a human), so this filter
    reads tuples rather than re-parsing English.  Keys are sorted so the
    readout is stable across runs -- a set has no order to inherit.

    The registry decides who is heard: ids in :data:`NO_OP_EFFECTS`,
    :data:`WEAPON_EFFECTS` and :data:`GANG_BUFF_EFFECTS` are silent by design,
    :data:`SPECIAL_STAT_EFFECTS` speak with their own wording, and anything
    else is reported by name and id so an SDE change surfaces instead of
    quietly zeroing a stat.
    """
    lines = []
    partial = False
    by_type = _items_by_type(fit)
    for key in sorted(fit.unmodeled_keys, key=lambda k: tuple(map(str, k))):
        kind = key[0]
        if kind == "type":
            lines.append(f"{name(key[1])}: unknown type")
        elif kind == "subsystem":
            lines.append(f"{name(key[1])}: subsystem not simulated")
        elif kind == "buff":
            lines.append(f"unknown warfare buff {key[1]}")
            partial = True
        elif kind == "effect":
            type_id, effect_id = key[1], key[2]
            if effect_id in NO_OP_EFFECTS or effect_id in WEAPON_EFFECTS \
                    or effect_id in GANG_BUFF_EFFECTS:
                continue
            item = by_type.get(type_id)
            if item is not None and \
                    item.category_id not in REPORTABLE_CATEGORIES:
                continue
            label = SPECIAL_STAT_EFFECTS.get(effect_id)
            if label is not None:
                lines.append(f"{name(type_id)}: {label}")
                partial = True
                continue
            lines.append(f"{name(type_id)}: effect {effect_id}")
            if _is_weapon_like(item):
                partial = True
        else:                                            # pragma: no cover
            lines.append(str(key))
    return lines, partial


# ===========================================================================
# derivation
# ===========================================================================

def derive(fit, profile: DamageProfile = OMNI, *, links: str = TIER_NONE,
           disciplines: Sequence[str] = (),
           name_of: Callable[[int], str] | None = None) -> FitStats:
    """Read DPS / volley / range / EHP off an ALREADY EVALUATED fit.

    ``fit`` must have been through :func:`fit_sim.evaluate` -- this function
    only reads attributes, it never applies a modifier.  ``name_of`` resolves
    type ids for the readout (consumers pass ``type_catalog.resolve_name``);
    without one every name reads ``type <id>``.

    Never raises for fit data: an empty, weaponless or entirely unknown fit
    yields zeros plus an ``unmodeled`` list.
    """
    name = _namer(name_of)
    dps_turret, dps_missile, weapon_volley, weapon_rows, missing = \
        _weapon_stats(fit, name)
    dps_drone, drone_volley, drone_rows = _drone_stats(fit, name)

    lines, partial = _unmodeled_entries(fit, name)
    if missing:
        # A weapon with no ammo is the loudest hole there is: first in the list.
        lines = list(missing) + lines
        partial = True

    layers = _layer_stats(fit.ship, profile)
    resists = {layer: values[2] for layer, values in layers.items()}
    try:
        build = dogma_data.sde_build()
    except dogma_data.DogmaUnavailable:                   # pragma: no cover
        build = 0

    return FitStats(
        dps_total=dps_turret + dps_missile + dps_drone,
        dps_turret=dps_turret,
        dps_missile=dps_missile,
        dps_drone=dps_drone,
        volley=weapon_volley + drone_volley,
        ranges=_weapon_ranges(weapon_rows + drone_rows),
        hp_shield=layers["shield"][0],
        hp_armor=layers["armor"][0],
        hp_hull=layers["hull"][0],
        ehp_shield=layers["shield"][1],
        ehp_armor=layers["armor"][1],
        ehp_hull=layers["hull"][1],
        ehp_total=(layers["shield"][1] + layers["armor"][1]
                   + layers["hull"][1]),
        resists=resists,
        links=links,
        disciplines_applied=tuple(disciplines),
        unmodeled=tuple(lines),
        sde_build=build,
        partial=partial,
    )


# ===========================================================================
# the facade + its bounded LRU
# ===========================================================================

#: ``(content hash, links, disciplines, profile) -> FitStats``, oldest first.
#: Bounded at :data:`CACHE_SIZE`; ``_cache_lock`` guards it because several
#: worker threads (the fit readout and the fleet aggregator) share it.
_cache: OrderedDict = OrderedDict()
_cache_lock = threading.Lock()


def clear_cache() -> None:
    """Drop every memoised result.  Call after a table reload."""
    with _cache_lock:
        _cache.clear()


def simulate(parsed: ParsedFit, *, links: str = TIER_NONE,
             disciplines: str = "auto", profile: DamageProfile = OMNI,
             name_of: Callable[[int], str] | None = None) -> FitStats:
    """Simulate one parsed fit at All-V skills.  The single public entry point.

    Cached on ``(fit_content_hash(parsed), links, disciplines, profile)``: the
    content hash is order-independent, so re-sorting a fit's modules is a cache
    HIT.  ``name_of`` is deliberately NOT part of the key -- it only decides
    cosmetic strings, and every consumer in the app shares one catalog.

    ``disciplines`` is the MODE string, one of
    :data:`fit_sim_links.DISCIPLINE_MODES` -- the MODE, not the resolved
    disciplines, is what enters the cache key.  That is sound because the mode
    resolves DETERMINISTICALLY for a given fit: ``auto`` reads the fit's own
    shield-vs-armor HP, and the fit is already pinned by its content hash.
    :attr:`FitStats.disciplines_applied` reports what the mode resolved to.

    **Cost.** One ``build_fit`` always, and ONE ``evaluate`` -- except for
    ``links != "none"`` with ``disciplines="auto"``, which needs TWO: the first
    settles the receiver's own fitted HP so ``auto`` can read the tank the
    pilot actually built, the second re-folds it with the buffs applied.
    ``evaluate`` rebuilds every value from ``base``, so the second pass is a
    clean re-evaluation, not an accumulation on top of the first.

    Raises :class:`ValueError` for an unknown tier or discipline mode, and
    :class:`dogma_data.DogmaUnavailable` when no table is loaded -- this never
    calls ``dogma_data.load()`` itself: that decode belongs to the caller's
    worker thread.
    """
    if links not in fit_sim_links.TIERS:
        raise ValueError(f"unknown link tier {links!r}; expected one of "
                         f"{list(fit_sim_links.TIERS)}")
    # Validated even for the no-links tier, which never resolves it: a typo in
    # the mode is a caller bug either way, and it must not lie dormant until
    # someone switches the tier on.
    if disciplines not in fit_sim_links.DISCIPLINE_MODES:
        raise ValueError(
            f"unknown discipline mode {disciplines!r}; expected one of "
            f"{list(fit_sim_links.DISCIPLINE_MODES)}")
    if not dogma_data.is_loaded():
        raise dogma_data.DogmaUnavailable(
            "no dogma table loaded -- call dogma_data.load() from a worker "
            "thread and honour a False result")

    key = (fit_content_hash(parsed), links, disciplines, profile)
    with _cache_lock:
        hit = _cache.get(key)
        if hit is not None:
            _cache.move_to_end(key)
            return hit

    fit = fit_sim.build_fit(parsed)
    if links == TIER_NONE:
        # No booster, no discipline: the mode is validated but never resolved,
        # so the no-links tier costs exactly one evaluation.
        applied: tuple = ()
        buffs: tuple = ()
    else:
        if disciplines == fit_sim_links.MODE_AUTO:
            fit_sim.evaluate(fit)               # the receiver at rest
        applied = fit_sim_links.choose_disciplines(fit, disciplines)
        buffs = fit_sim_links.buffs_for(links, applied)

    stats = derive(fit_sim.evaluate(fit, buffs), profile,
                   links=links, disciplines=applied, name_of=name_of)

    with _cache_lock:
        _cache[key] = stats
        _cache.move_to_end(key)
        while len(_cache) > CACHE_SIZE:
            _cache.popitem(last=False)
    return stats
