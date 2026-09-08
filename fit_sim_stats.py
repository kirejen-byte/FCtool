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

:class:`FitStats` is FROZEN and HASHABLE -- every field is a tuple or a scalar,
including ``resists`` (see :meth:`FitStats.resists_map` for the dict view).  A
cached result is therefore safe to hand to two consumers at once: neither can
mutate what the other reads.

``simulate``'s ``name_of`` is a REQUIRED keyword.  The cache is shared and the
rendered strings are NOT part of its key, so whichever caller misses first
decides the names every later caller sees.  Passing it explicitly makes that a
deliberate choice rather than a race -- every app consumer passes the single
``TypeCatalog.resolve_name``, so the cached strings are consistent.  A caller
that genuinely wants raw ids passes ``name_of=None``.  For anything that must
re-resolve a name itself, :attr:`FitStats.unmodeled_items` carries the same
report as ``(type_id, reason code)`` pairs, index-parallel to ``unmodeled``.

**Nothing loads at startup.**  :func:`simulate` raises
:class:`dogma_data.DogmaUnavailable` when no table is installed; it never calls
``dogma_data.load()`` itself, because that is a multi-megabyte decode and the
caller's worker thread owns the decision to pay for it.

Two OWNER RULES shape the headline numbers (spec 4.1, recorded verbatim there):

* **Ammo.**  ``simulate(..., ammo="best_close")`` -- the DEFAULT -- ignores what
  the EFT loaded and assumes the highest-DPS CLOSE-RANGE non-Tech-II charge for
  every weapon group, chosen from data and confirmed by SIMULATION (a
  kinetic-bonused hull picks its kinetic ammo by itself).  ``"as_fitted"`` keeps
  the pilot's own charges.  :attr:`FitStats.ammo_assumed` names what was chosen.
* **Drones.**  Drone damage joins the headline ``dps_total`` / ``volley`` ONLY
  when it is more than half the fit's raw total -- a primarily-drone ship.  The
  breakdown (:attr:`FitStats.dps_drone`) always reports it, and
  :attr:`FitStats.dps_raw_total` / :attr:`FitStats.volley_raw` carry the
  everything-counted numbers, so the readout can say "(drones excluded)" rather
  than quietly showing a smaller number.

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
from fit_models import ParsedFit, ParsedModule, fit_content_hash

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
    # Ballistic Control Systems (and any other missile damage rig/implant) do
    # NOT land on the launcher or the charge -- effect 763 (``missileDMGBonus``,
    # BCS modules) and effect 2851 (Warhead Calefaction Catalyst rigs/boosters)
    # are both ``charID``/``ItemModifier`` rows that multiply attribute 212 on
    # the CHARACTER item itself (verified against dogmaEffects.jsonl; default
    # 1.0, non-stackable, so 3x BCS II stacking-penalises).  Turret and drone
    # damage bonuses never take this shape -- every character-domain modifier
    # that touches their own ``damageMultiplier`` (64) does so via
    # ``OwnerRequiredSkillModifier`` reaching the DRONE items themselves (an
    # already-implemented domain), so this attribute is missile-only.
    "missileDamageMultiplier": 212,
    # ammo selection (spec 4.1.1) -- read off the WEAPON to find its charges,
    # and off a CHARGE to tell close range from long.  ``chargeSize`` is absent
    # on every launcher (missiles are sized by their own group), which is why
    # the size filter is conditional; ``weaponRangeMultiplier``'s dogma default
    # is 1.0, and close-range ammo publishes a value BELOW it (0.5 for Fusion /
    # EMP / Antimatter / Multifrequency), which is what makes "closer range" a
    # data question rather than a name one.
    "chargeSize": 128,
    "weaponRangeMultiplier": 120,
    # drones
    "droneBandwidth": 1271,                 # on the ship
    "droneBandwidthUsed": 1272,             # on the drone
    "maxActiveDrones": 352,                 # on the ship
    # TANK MARKERS -- what a module DECLARES when it is tank gear.  Read off
    # real SDE rows, not guessed (1600mm Steel Plates II 20353 -> 1159; Large
    # Shield Extender II 3841 -> 72, which the SDE calls capacityBonus, NOT
    # shieldCapacityBonus; Multispectrum Energized Membrane II 11269 -> 984-7;
    # Damage Control II 2048 and the Reactive Armor Hardener 4403 -> the
    # resonances themselves).  ``hp`` / ``shieldCapacity`` / ``armorHP`` are
    # deliberately NOT tank markers: every module in the game carries ``hp``.
    "capacityBonus": 72,                    # shield extenders (SDE's own name)
    "shieldCapacityMultiplier": 146,
    "armorHPMultiplier": 148,
    "structureHPMultiplier": 150,
    "hullHpBonus": 327,
    "armorHpBonus": 335,
    "shieldCapacityBonus": 337,
    "emDamageResistanceBonus": 984,
    "explosiveDamageResistanceBonus": 985,
    "kineticDamageResistanceBonus": 986,
    "thermalDamageResistanceBonus": 987,
    "armorHPBonusAdd": 1159,
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

#: Every resonance attribute, as a flat set -- a hardener/membrane/damage
#: control declares them as its OWN attributes, which is what finds them.
RESONANCE_ATTRS = frozenset(
    attr_id for _layer, _hp, resonances in LAYER_SPECS
    for attr_id in resonances)

#: A module carrying ANY of these is tank gear, so an effect the engine could
#: not model on it means the EHP number is wrong (spec: ``partial``). Membership
#: is tested against the module's BASE attributes -- what its type declares --
#: never against a modified value, which every hull would incidentally have.
TANK_ATTRS = frozenset(RESONANCE_ATTRS | {
    ATTR["capacityBonus"], ATTR["shieldCapacityBonus"],
    ATTR["shieldCapacityMultiplier"], ATTR["armorHpBonus"],
    ATTR["armorHPBonusAdd"], ATTR["armorHPMultiplier"],
    ATTR["hullHpBonus"], ATTR["structureHPMultiplier"],
    ATTR["emDamageResistanceBonus"], ATTR["explosiveDamageResistanceBonus"],
    ATTR["kineticDamageResistanceBonus"], ATTR["thermalDamageResistanceBonus"],
})

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

# ===========================================================================
# the ammo policy (spec 4.1.1 -- the owner's first assumption)
# ===========================================================================
# "Default DPS numbers are with the highest DPS close range non-T2 ammunition
# (Republic Fleet EMP, Antimatter, Multifrequency, Navy missiles)."  Everything
# below is that sentence turned into data questions: which charges FIT this gun
# (its chargeGroups and chargeSize), which of them are non-T2 (meta group), and
# which one actually produces the most DPS ON THIS HULL (simulated, so a
# damage-type bonus decides for itself instead of being hard-coded).

#: Assume the best close-range non-T2 charge for every weapon group.
AMMO_BEST_CLOSE = "best_close"
#: Keep whatever the EFT loaded -- what every caller got before the policy.
AMMO_AS_FITTED = "as_fitted"
AMMO_MODES = (AMMO_BEST_CLOSE, AMMO_AS_FITTED)

#: ``chargeGroup1..5``.  NON-contiguous by CCP's own numbering (604, 605, 606,
#: then 609, 610) -- a ``range(604, 609)`` would silently drop two of them, and
#: a hybrid turret keeps its Charges in the ones a naive range misses.
CHARGE_GROUP_ATTRS = (604, 605, 606, 609, 610)

#: The meta groups a default charge may come from: 0 (none published), 1 Tech I
#: and 4 faction.  NEVER 2 -- Tech II ammo carries a real drawback (less range,
#: worse tracking) and the owner's rule says non-T2 -- and never 3/5/6/14/15
#: (storyline, officer, deadspace, Tech III, abyssal), which no fleet undocks
#: with.  0 IS ALLOWED and that is load-bearing: 85 category-8 charges publish
#: no meta group at all and 11 of them deal damage, including the entire T1
#: exotic-plasma line (Baryon S/M/L) and EVERY XL exotic plasma.  Excluding 0
#: left the Zirnitra's Ultratidal Entropic Disintegrator with zero candidates.
#: Zero-damage members of that band (bombs' payloads aside, probes, scripts)
#: drop on the damage filter instead, where they belong.
AMMO_META_GROUPS = frozenset({0, 1, 4})

#: The only factions a meta-4 (faction) charge may come from: the four empire
#: navies.  The SDE gives navy AND pirate ammunition the SAME meta group 4 and
#: publishes no ``factionID`` on charges at all, so without this set the policy
#: picks Domination EMP L over Republic Fleet EMP L and Dread Guristas Scourge
#: over Caldari Navy Scourge -- ~4 % more damage, a tier the owner did not name
#: and a fleet does not actually fly.  Charge factions are name-derived by
#: ``tools/gen_fit_dogma.CHARGE_FACTION_NAME_PREFIXES``; an unclassified faction
#: charge reads 0 and so fails this whitelist, which is the safe direction.
#: Meta 0 and meta 1 charges need no faction -- T1 ammo owns none.
AMMO_NAVY_FACTIONS = frozenset({500001, 500002, 500003, 500004})

#: The meta group :data:`AMMO_NAVY_FACTIONS` gates -- "Faction" in the SDE's own
#: ``metaGroups`` table.  Named so the gate below reads as the rule it is.
AMMO_FACTION_META_GROUP = 4

#: How many of the highest-raw-damage candidates are actually SIMULATED.  Four
#: is the number that makes the hull decide: a faction ammo line has three or
#: four damage-type variants tied on raw total (EMP / Fusion / Phased Plasma;
#: Scourge / Inferno / Mjolnir / Nova), so the top four are exactly "one line's
#: worth of choices" and the fit's own bonuses break the tie.  Each candidate
#: costs one ``build_fit`` + ``evaluate``, so this is also the cost knob:
#: MEASURED cold on the owner's Machariel (7 guns, one group), 16 ms
#: ``as_fitted`` -> 77 ms ``best_close``, median of 5.  It is paid once per
#: fit on a worker thread and then held by the results LRU.
AMMO_CANDIDATES = 4

#: Two simulated DPS numbers this close are the same number (float noise from a
#: chain of multipliers), so the tie-breaks below decide instead.
AMMO_DPS_EPSILON = 1e-9

#: The LAST tie-break before the type id: the owner's own vocabulary, in his
#: order.  Only ever consulted when the simulated DPS and the range multiplier
#: are both tied -- i.e. between the damage-type variants of ONE ammo line on a
#: hull with no damage-type bonus, where every choice is equally right and only
#: the printed name differs.  It reads the RESOLVED name, so a caller that
#: passed ``name_of=None`` simply falls through to the type id.
CLOSE_RANGE_NAME_PREFERENCE = ("EMP", "Antimatter", "Multifrequency",
                               "Scourge", "Inferno", "Mjolnir", "Nova")

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
    """One weapon group's reach.

    ``falloff_m`` is ``0.0`` whenever the item does not DECLARE a falloff:
    attribute 158's dogma default is 1.0 (verified against
    ``dogmaAttributes.jsonl``), so a launcher read through :func:`fit_sim.attr`
    would otherwise report a one-metre falloff.  A missile's ``optimal_m`` is
    its maximum flight range.

    ``loaded`` is False for a turret or launcher with no charge: the row is
    still reported (the pilot fitted the gun, and its RANGE is real) but it
    contributes no DPS, and the fit is ``partial``.
    """
    name: str
    type_id: int
    count: int
    kind: str                       # "turret" | "missile" | "drone"
    optimal_m: float
    falloff_m: float
    loaded: bool = True


@dataclass(frozen=True)
class FitStats:
    """Everything the readout shows for one fit, at All-V skills.

    Frozen AND hashable: every field is a scalar or a tuple, so a cached
    instance handed to two consumers cannot be mutated by either.  That is why
    ``resists`` is a tuple of pairs rather than the dict the spec sketched --
    :meth:`resists_map` gives the dict view for readability at the call site.
    """

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
    #: ``((layer, (em, th, kin, exp)), ...)`` in LAYER_SPECS order (shield,
    #: armor, hull); the four numbers are RESIST fractions, i.e.
    #: ``1 - resonance``.
    resists: tuple
    links: str
    disciplines_applied: tuple[str, ...]
    skills: str = "all_v"
    unmodeled: tuple[str, ...] = ()
    #: ``(type_id, reason code)`` for every ``unmodeled`` line, in the SAME
    #: order -- the machine-readable twin of the human strings, so a formatter
    #: can re-resolve a name without re-running the simulation.  Reason codes:
    #: ``unknown_type``, ``corrupt_row``, ``subsystem``, ``no_charge``,
    #: ``no_ammo_candidate`` (a weapon the ammo policy found nothing for),
    #: ``special_effect:<id>``, ``effect:<id>``, ``unknown_buff:<id>`` (type id
    #: 0 -- a buff id is not a type id), ``links_unavailable`` (also type id 0
    #: -- a link tier whose preset this SDE no longer carries),
    #: ``unknown_key:<kind>``.  Identical reports are FOLDED (five empty guns
    #: of one type are one line, prefixed ``5x``), so one entry here can stand
    #: for several fitted items.
    unmodeled_items: tuple[tuple[int, str], ...] = ()
    #: Diagnostics that are NOT gaps: a hull bonus whose modifiers simply found
    #: no matching module (a Raven's launcher bonus on a fit with no launcher),
    #: or an evaluation that hit the pass cap without settling.
    #: Never shown as a gap, never sets ``partial``.
    notes: tuple[str, ...] = ()
    sde_build: int = 0
    #: True when something the engine could not model would have moved a
    #: number: a weapon, charge, drone or tank module, an unfitted charge, a
    #: vanished fitted type, or one of SPECIAL_STAT_EFFECTS.
    partial: bool = False
    #: DPS and volley with EVERYTHING counted, drones included, whatever the
    #: drone rule decided.  ``dps_total`` is these minus the drones when
    #: :attr:`drones_counted` is False, so a readout can show both without
    #: re-deriving anything.
    dps_raw_total: float = 0.0
    volley_raw: float = 0.0
    #: Whether drone damage is IN ``dps_total`` / ``volley``: the owner's rule
    #: is "only when the ship is primarily a drone ship", i.e. drone DPS is
    #: strictly more than half the raw total.  A fit with no DPS at all is
    #: False -- there is nothing to be more than half of.
    drones_counted: bool = False
    #: The ammo mode these numbers were computed under, one of
    #: :data:`AMMO_MODES`.
    ammo: str = AMMO_AS_FITTED
    #: ``((weapon type id, charge type id, charge name), ...)`` -- what
    #: ``best_close`` ASSUMED, one entry per weapon group in fit order.  Empty
    #: under ``as_fitted`` (nothing was assumed), and a weapon group the policy
    #: could not resolve is absent rather than listed with the pilot's charge:
    #: it kept what the EFT loaded, and says so through an ``unmodeled`` line.
    ammo_assumed: tuple[tuple[int, int, str], ...] = ()

    def resists_map(self) -> dict:
        """``{layer: (em, th, kin, exp)}`` -- the dict view of
        :attr:`resists`, built fresh so mutating it cannot poison the cache."""
        return dict(self.resists)


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

def _weapon_kind_from_effects(effects) -> str | None:
    """"turret" / "missile" / None for a bare effect-id sequence.

    The hardpoint markers win: ``turretFitted`` / ``launcherFitted`` are the
    game's own bookkeeping.  Only then do the classifier effects speak, so a
    launcher that also carries ``barrage``-flavoured ammo cannot be misread.
    An explicit ``None`` in :data:`WEAPON_EFFECTS` (a defender missile, a probe
    launcher) is not a weapon this layer counts.

    Split out from :func:`_weapon_kind` so the ammo policy -- which classifies
    from the TABLE, before any item exists -- shares this one rule rather than
    growing a second copy that could drift from it.
    """
    if TURRET_FITTED in effects:
        return KIND_TURRET
    if LAUNCHER_FITTED in effects:
        return KIND_MISSILE
    for effect_id in effects:
        kind = WEAPON_EFFECTS.get(effect_id)
        if kind:
            return kind
    return None


def _weapon_kind(item) -> str | None:
    """"turret" / "missile" / None for one fitted module.

    Reads ``Item.effects`` -- every effect the type has -- and NEVER
    ``Item.unmodeled_effects``: a classifier that does carry modifierInfo never
    lands in the unmodelled list, so reading that list would stop recognising
    the gun.
    """
    return _weapon_kind_from_effects(item.effects)


def _damage_sum(item) -> float:
    """The four damage types of one charge or drone, added.

    Read through :func:`fit_sim.attr` so an absent attribute answers with its
    dogma DEFAULT rather than a bare zero -- the table stores only values that
    differ from the default, so ``attrs.get`` is wrong by construction."""
    return sum(fit_sim.attr(item, attr_id) for attr_id in DAMAGE_ATTRS)


def _falloff_m(item) -> float:
    """Falloff in metres, or 0.0 when the item does not declare one.

    Attribute 158's dogma DEFAULT is 1.0 (verified against the SDE's
    ``dogmaAttributes.jsonl``), not 0.0 -- so :func:`fit_sim.attr` answers "1"
    for every launcher, missile and drone that has no falloff at all.  Absence
    is read off the stores (``base`` = what the type declares, ``attrs`` = what
    a modifier wrote), never off the VALUE: a real 1 m falloff and a missing
    one are the same number.
    """
    attr_id = ATTR["falloff"]
    if attr_id not in item.base and attr_id not in item.attrs:
        return 0.0
    return fit_sim.attr(item, attr_id)


def _is_tank_module(item) -> bool:
    """Whether this item's own attributes make it tank gear, so an effect the
    engine could not model on it means the EHP number is wrong.

    MODULES only: the hull declares HP and every resonance by definition, and a
    rig/module declares one of :data:`TANK_ATTRS` only when it is there to
    change the tank.  Read off ``base`` -- ``attrs`` carries whatever a
    modifier wrote, which would make half the fit look like a plate.
    """
    if item is None or item.category_id != fit_sim.CATEGORY_MODULE:
        return False
    return not TANK_ATTRS.isdisjoint(item.base)


def _cycle_seconds(item) -> float:
    """Rate of fire in seconds.  Attribute 51 ``speed`` is milliseconds for
    turrets, launchers AND drones (spec A.7: there is no ``rateOfFire``)."""
    return fit_sim.attr(item, ATTR["speed"]) / 1000.0


def _missile_damage_multiplier(character) -> float:
    """The character's BCS-and-friends bonus to missile damage.

    Effect 763 (``missileDMGBonus``, the SDE effect every Ballistic Control
    System carries) is a ``charID``/``ItemModifier`` that multiplies attribute
    212 on the CHARACTER item itself -- not the launcher, not the charge --
    which is why :func:`_module_volley` cannot read it off either weapon item.
    ``character`` is ``None`` only for a hand-built :class:`fit_sim.FitState`
    that skipped :func:`fit_sim.build_fit` (production always populates it);
    the dogma default for attribute 212 is 1.0, so that is the honest answer
    for "no character item" too.
    """
    if character is None:
        return 1.0
    return fit_sim.attr(character, ATTR["missileDamageMultiplier"])


def _module_volley(module, kind: str, character) -> float:
    """One LOADED weapon's volley.

    Turret volley = the charge's damage times the GUN's multiplier; the engine
    has already folded skills, ship bonuses and the charge's own ``otherID``
    modifiers into both numbers.  Missile damage lives almost entirely on the
    charge (the engine applied the missile skills to it through
    ``OwnerRequiredSkillModifier``), so multiplying by the launcher would
    double-count -- but Ballistic Control Systems land on the CHARACTER
    (:func:`_missile_damage_multiplier`), which nothing else folds in, so it
    is applied here explicitly.

    The single owner of that formula: the readout and the ammo policy's
    candidate scoring both go through here, so a candidate is ranked by exactly
    the number the readout will print.
    """
    if kind == KIND_TURRET:
        return _damage_sum(module.charge) * fit_sim.attr(
            module, ATTR["damageMultiplier"])
    return _damage_sum(module.charge) * _missile_damage_multiplier(character)


def _module_dps(module, kind: str, character) -> float:
    """One loaded weapon's DPS -- volley over its cycle, 0.0 for a cycle-less
    (and so unfireable) module rather than a division by zero."""
    cycle = _cycle_seconds(module)
    if cycle <= 0:
        return 0.0
    return _module_volley(module, kind, character) / cycle


def _weapon_stats(fit, name):
    """Per-weapon DPS / volley / ranges for the fitted modules.

    Returns ``(dps_turret, dps_missile, volley, range_rows, missing_charge)``,
    where ``missing_charge`` is a list of ``(type_id, name)``.
    An offline module contributes nothing (the engine already gives it no
    effects; DPS follows the same rule).  A turret or launcher with NO charge
    contributes zero DPS and is reported -- that is a real hole in the number,
    so it also sets ``partial`` -- but it STILL yields a range row (marked
    ``loaded=False``): the pilot fitted the gun, and the gun's own optimal and
    falloff are real numbers the readout should show.
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
            missing_charge.append((module.type_id, module_name))
            # The module's OWN reach -- a launcher declares no maxRange, which
            # reads as the attribute's 0.0 default, and that is the honest
            # answer for a launcher with nothing in it.
            rows.append((module.type_id, module_name, kind,
                         fit_sim.attr(module, ATTR["maxRange"]),
                         _falloff_m(module), 1, False))
            continue

        volley = _module_volley(module, kind, fit.character)
        if kind == KIND_TURRET:
            optimal = fit_sim.attr(module, ATTR["maxRange"])
            falloff = _falloff_m(module)
        else:
            optimal = (fit_sim.attr(charge, ATTR["maxVelocity"])
                       * fit_sim.attr(charge, ATTR["explosionDelay"]) / 1000.0)
            falloff = 0.0

        dps = _module_dps(module, kind, fit.character)
        if kind == KIND_TURRET:
            dps_turret += dps
        else:
            dps_missile += dps
        volley_total += volley
        rows.append((module.type_id, module_name, kind, optimal, falloff, 1,
                     True))

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
        rows.append((drone.type_id, name(drone.type_id), KIND_DRONE,
                     fit_sim.attr(drone, ATTR["maxRange"]),
                     _falloff_m(drone), active, True))

    return dps_total, volley_total, rows


def _weapon_ranges(rows) -> tuple[WeaponRange, ...]:
    """Fold identical weapon rows into counted groups, first-seen order.

    The type id is part of the key, so two DIFFERENT guns that happen to share
    a resolved name and a reach stay two rows; ``loaded`` is too, so a loaded
    and an empty gun of the same type never merge into one misleading count.
    """
    grouped: OrderedDict = OrderedDict()
    for type_id, name, kind, optimal, falloff, count, loaded in rows:
        key = (type_id, name, kind, round(optimal, 3), round(falloff, 3),
               loaded)
        grouped[key] = grouped.get(key, 0) + count
    return tuple(WeaponRange(name=key[1], type_id=key[0], count=count,
                             kind=key[2], optimal_m=key[3], falloff_m=key[4],
                             loaded=key[5])
                 for key, count in grouped.items())


# ===========================================================================
# ammo: choosing the best close-range non-T2 charge (spec 4.1.1)
# ===========================================================================

def _type_attr(attrs: dict, attr_id: int) -> float:
    """One attribute off a RAW type-attribute mapping, dogma default filled.

    The table stores only values that DIFFER from the attribute default, so
    ``attrs.get(id, 0.0)`` is wrong by construction here exactly as it is on an
    :class:`fit_sim.Item` -- ``weaponRangeMultiplier``'s default is 1.0, and
    reading an absent one as 0.0 would make every long-range charge look like
    the closest-range one there is.
    """
    value = attrs.get(attr_id)
    if value is None:
        return dogma_data.attr_info(attr_id).default
    return float(value)


def _type_attrs_or_empty(type_id: int) -> dict:
    """A type's own attributes, or ``{}`` for an unknown or corrupt row.

    The ammo policy walks whole GROUPS of the table, so it meets rows the fit
    itself never named: an unreadable one is a candidate that does not exist,
    never an exception out of ``simulate``."""
    try:
        return dogma_data.type_attrs(type_id)
    except (KeyError, ValueError):
        return {}


def _raw_damage_sum(attrs: dict) -> float:
    """The four damage types of a charge, added, straight off its type row.

    "Raw" is the point: this ranks candidates BEFORE any hull bonus or skill
    exists, purely to pick which handful are worth simulating."""
    return sum(_type_attr(attrs, attr_id) for attr_id in DAMAGE_ATTRS)


def _declared_charge_groups(weapon_type_id: int) -> tuple[int, ...]:
    """The group ids this weapon accepts, first-declared order, deduped.

    An EMPTY result means the table publishes no ``chargeGroup`` at all for the
    type -- there is no question to ask, so the policy leaves that weapon's own
    charge alone and says nothing (every real gun and launcher declares at
    least ``chargeGroup1``; a weapon-shaped module that does not is one whose
    ammunition the table cannot describe)."""
    attrs = _type_attrs_or_empty(weapon_type_id)
    groups: list[int] = []
    for attr_id in CHARGE_GROUP_ATTRS:
        group_id = int(attrs.get(attr_id) or 0)
        if group_id and group_id not in groups:
            groups.append(group_id)
    return tuple(groups)


def _charge_candidates(weapon_type_id: int) -> tuple[int, ...]:
    """The ``AMMO_CANDIDATES`` best close-range non-T2 charges for one weapon.

    Five filters, each of them a data question:

    * the charge is in one of the weapon's own ``chargeGroup`` groups AND is
      category 8 (a group can hold non-charges);
    * its meta group is 0, 1 or 4 -- unclassified, Tech I or faction, never
      Tech II (:data:`AMMO_META_GROUPS`);
    * if it IS faction (meta 4), its faction is one of the four empire navies
      (:data:`AMMO_NAVY_FACTIONS`) -- the SDE files pirate ammo under the same
      meta group, and the owner's tier is the navy one;
    * its ``chargeSize`` matches the weapon's, WHEN the weapon publishes one --
      launchers do not (a missile's size is its group), so for them the filter
      is skipped rather than made to reject everything;
    * it does damage at all, which is what drops scripts, probes and every
      other utility "charge" that shares a group with real ammunition.

    Ranked by raw damage descending (type id as the stable secondary key) and
    CAPPED: the simulation that follows is the expensive half, and everything
    below the cap is a strictly worse starting point than the four above it.
    """
    groups = _declared_charge_groups(weapon_type_id)
    if not groups:
        return ()
    weapon_size = _type_attrs_or_empty(weapon_type_id).get(ATTR["chargeSize"])
    ranked: list[tuple[float, int]] = []
    seen: set = set()
    for group_id in groups:
        for type_id in dogma_data.types_in_group(group_id):
            if type_id in seen:
                continue
            seen.add(type_id)
            try:
                if dogma_data.type_category(type_id) != fit_sim.CATEGORY_CHARGE:
                    continue
                meta_group = dogma_data.type_meta(type_id)
                if meta_group not in AMMO_META_GROUPS:
                    continue
                if (meta_group == AMMO_FACTION_META_GROUP
                        and dogma_data.type_faction(type_id)
                        not in AMMO_NAVY_FACTIONS):
                    continue
            except (KeyError, ValueError):                # pragma: no cover
                continue
            attrs = _type_attrs_or_empty(type_id)
            if weapon_size and attrs.get(ATTR["chargeSize"]) != weapon_size:
                continue
            damage = _raw_damage_sum(attrs)
            if damage <= 0:
                continue
            ranked.append((damage, type_id))
    ranked.sort(key=lambda row: (-row[0], row[1]))
    return tuple(type_id for _damage, type_id in ranked[:AMMO_CANDIDATES])


def _weapon_type_ids(parsed: ParsedFit) -> tuple[int, ...]:
    """The fit's weapon GROUPS -- one entry per weapon type, in fit order.

    Classified from the table, not from a built fit: the policy has to choose
    the ammo BEFORE the fit exists.  Offline weapons are skipped for the same
    reason the readout skips them -- an offline gun fires nothing, so assuming
    ammunition for it would buy a simulation pass and change no number.
    """
    weapons: list[int] = []
    seen: set = set()
    for parsed_module in parsed.modules:
        type_id = int(parsed_module.type_id)
        if parsed_module.offline or type_id in seen:
            continue
        try:
            effects = dogma_data.type_effects(type_id)
        except (KeyError, ValueError):
            continue
        if _weapon_kind_from_effects(effects) is None:
            continue
        seen.add(type_id)
        weapons.append(type_id)
    return tuple(weapons)


def _reloaded(parsed: ParsedFit, charges: dict) -> ParsedFit:
    """``parsed`` with every weapon group's charge replaced by ``charges``.

    A NEW :class:`fit_models.ParsedFit` -- the caller's fit is the library's
    own object and is never mutated (it is also what the results cache is keyed
    on, so writing through it would poison the key).  Offline modules keep what
    the pilot loaded: they are not in ``charges`` and fire nothing anyway.
    """
    if not charges:
        return parsed
    modules = []
    changed = False
    for parsed_module in parsed.modules:
        charge_id = (None if parsed_module.offline
                     else charges.get(int(parsed_module.type_id)))
        if charge_id is None or charge_id == parsed_module.charge_type_id:
            modules.append(parsed_module)
            continue
        changed = True
        modules.append(ParsedModule(
            type_id=parsed_module.type_id, name=parsed_module.name,
            slot=parsed_module.slot, charge_type_id=int(charge_id),
            charge_name=None, offline=parsed_module.offline))
    if not changed:
        return parsed
    return ParsedFit(ship_type_id=parsed.ship_type_id,
                     ship_name=parsed.ship_name, modules=modules,
                     drones=parsed.drones, cargo=parsed.cargo,
                     subsystems=parsed.subsystems,
                     name_hint=parsed.name_hint)


def _group_dps(fit, weapon_type_id: int) -> float:
    """The DPS ONE weapon group contributes to an evaluated fit.

    Scoring the group rather than the whole fit is what keeps a multi-group fit
    honest: the other groups are loaded too (so hull bonuses and stacking are
    real), but only this group's number decides this group's ammo.
    """
    total = 0.0
    for module in fit.modules:
        if module.type_id != weapon_type_id:
            continue
        if module.state == fit_sim.STATE_OFFLINE or module.charge is None:
            continue
        kind = _weapon_kind(module)
        if kind is None:
            continue
        total += _module_dps(module, kind, fit.character)
    return total


def _best_candidate(scored, name) -> int:
    """The winning charge from ``[(dps, type id), ...]``.

    Highest simulated DPS, then -- for the variants that tie, which on a hull
    with no damage-type bonus is all of them -- the CLOSER-range one
    (``weaponRangeMultiplier`` ascending), then the owner's own name order, then
    the lowest type id.  Every step is total, so the choice is deterministic:
    a readout whose assumed ammo changed between two identical runs would be
    unreportable.
    """
    best = max(dps for dps, _type_id in scored)
    tied = [type_id for dps, type_id in scored
            if abs(dps - best) <= AMMO_DPS_EPSILON]
    if len(tied) == 1:
        return tied[0]
    return min(tied, key=lambda type_id: (
        _type_attr(_type_attrs_or_empty(type_id),
                   ATTR["weaponRangeMultiplier"]),
        _name_preference(name(type_id)),
        type_id))


def _name_preference(charge_name: str) -> int:
    """Where a charge's resolved name sits in
    :data:`CLOSE_RANGE_NAME_PREFERENCE`; past the end when it names none of
    them (which is what an unresolved ``type <id>`` does)."""
    for index, token in enumerate(CLOSE_RANGE_NAME_PREFERENCE):
        if token in charge_name:
            return index
    return len(CLOSE_RANGE_NAME_PREFERENCE)


def _choose_ammo(parsed: ParsedFit, name):
    """``(charges, unavailable, assumed)`` for the ``best_close`` policy.

    ``charges`` maps weapon type id -> chosen charge type id; ``unavailable``
    names the weapon groups nothing could be found for; ``assumed`` is the
    report :attr:`FitStats.ammo_assumed` carries.

    Groups are resolved ONE AT A TIME in fit order, each candidate simulated
    with the already-decided groups carrying their choice and the undecided
    ones carrying their top-raw candidate.  That ordering matters on a mixed
    fit: a stacking-penalised damage modifier makes a group's DPS depend on
    what the rest of the fit is shooting, and evaluating each group in a void
    would score it against a fit that does not exist.

    **Links-independent by construction.**  The choice is made on the BARE fit,
    before any warfare buff is applied, because no v1 dbuff touches a weapon
    attribute (they move shield/armor HP and resonances).  So the ``auto``
    discipline pre-pass never re-runs this search, and a linked and an unlinked
    simulation of one fit assume the same ammunition -- asserted by a test.
    """
    weapons = _weapon_type_ids(parsed)
    if not weapons:
        return {}, (), ()

    candidates = {type_id: _charge_candidates(type_id) for type_id in weapons}
    # A weapon that declares charge groups and still has no candidate is a real
    # hole (nothing T1/faction of the right group and size exists in this
    # table); one that declares NO groups is not a question the table can
    # answer, so it passes in silence with the pilot's own charge.
    unavailable = tuple(type_id for type_id in weapons
                        if not candidates[type_id]
                        and _declared_charge_groups(type_id))
    chosen = {type_id: found[0]
              for type_id, found in candidates.items() if found}

    for weapon_id in weapons:
        found = candidates[weapon_id]
        if len(found) < 2:
            # Nothing to compare: the single candidate (or none at all) is
            # already in ``chosen``, and an evaluate would only cost time.
            continue
        scored = []
        for candidate in found:
            chosen[weapon_id] = candidate
            evaluated = fit_sim.evaluate(
                fit_sim.build_fit(_reloaded(parsed, chosen)))
            scored.append((_group_dps(evaluated, weapon_id), candidate))
        chosen[weapon_id] = _best_candidate(scored, name)

    assumed = tuple((type_id, chosen[type_id], name(chosen[type_id]))
                    for type_id in weapons if type_id in chosen)
    return chosen, unavailable, assumed


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


def _effect_has_modifiers(effect_id) -> bool:
    """Whether the table declares modifiers for this effect.

    This is the ONE thing that tells the engine's two gap classes apart, since
    both land on the same ``("effect", type, effect)`` key:
    ``fit_sim._make_item`` records an effect with NO ``modifierInfo`` at all,
    while
    ``fit_sim._record_effect_gap`` records one whose modifiers resolved to no
    target.  Only the second can be a "reached nothing" note (see
    :func:`_unmodeled_entries`).
    """
    definition = dogma_data.effect(effect_id)
    return definition is not None and bool(definition.modifiers)


def _fold_repeats(lines, items):
    """Fold repeated reports into one counted line, first-seen order kept.

    The identity of a report is its ``(type id, reason code)`` pair plus the
    rendered line, so the fold is generic over every reason code rather than
    special-cased on the one that repeats today.  Five empty guns of one type
    are ONE hole in the numbers, and printing it five times pushes the rest of
    the report off a small readout.

    The line is part of the key because a reason code is not always unique on
    its own: ``links_unavailable`` carries type id 0 and names its discipline
    only in the text, so keying on the pair alone would fold a missing SHIELD
    link and a missing ARMOR link into one line that named a single discipline.
    A repeat is therefore a genuinely IDENTICAL report, never merely a
    similar one.

    ``lines`` and ``items`` come in index-parallel and go out index-parallel:
    each folded line keeps exactly one structured entry, so a formatter can
    still re-resolve the name behind a counted line.

    The count prefix is ``N`` U+00D7 (the multiplication sign) plus a space --
    "5x" would read as part of a module name.
    """
    folded_lines: list = []
    folded_items: list = []
    counts: list = []
    seen: dict = {}
    for line, item in zip(lines, items):
        key = (item, line)
        at = seen.get(key)
        if at is None:
            seen[key] = len(folded_items)
            folded_lines.append(line)
            folded_items.append(item)
            counts.append(1)
        else:
            counts[at] += 1
    return ([line if count == 1 else f"{count}× {line}"
             for line, count in zip(folded_lines, counts)], folded_items)


def _unmodeled_entries(fit, name):
    """``(lines, items, notes, partial)`` for everything the engine could not
    model.

    ``FitState.unmodeled_keys`` is the STRUCTURED record (the ``unmodeled``
    list is the same information already rendered for a human), so this filter
    reads tuples rather than re-parsing English.  Keys are sorted so the
    readout is stable across runs -- a set has no order to inherit.
    ``lines`` and ``items`` are built in lockstep and stay index-parallel.

    The registry decides who is heard: ids in :data:`NO_OP_EFFECTS`,
    :data:`WEAPON_EFFECTS` and :data:`GANG_BUFF_EFFECTS` are silent by design,
    :data:`SPECIAL_STAT_EFFECTS` speak with their own wording, and anything
    else is reported by name and id so an SDE change surfaces instead of
    quietly zeroing a stat.

    A HULL bonus whose modifiers found no target is not a gap at all -- a
    Raven's launcher bonus on a fit with no launcher is the engine working
    correctly, and the user can do nothing about it.  Those go to ``notes``
    and never set ``partial``.  A hull effect with no modifierInfo IS a gap
    (the table cannot express the bonus) and stays in ``lines``.
    """
    lines = []
    items = []
    notes = []
    partial = False
    by_type = _items_by_type(fit)

    def report(text, type_id, reason):
        lines.append(text)
        items.append((int(type_id), reason))

    for key in sorted(fit.unmodeled_keys, key=lambda k: tuple(map(str, k))):
        kind = key[0]
        if kind == "type":
            # A type the table does not know is a type that VANISHED from the
            # fit: whatever it was -- gun, plate, rig -- its contribution is
            # missing from every number below.
            report(f"{name(key[1])}: unknown type", key[1], "unknown_type")
            partial = True
        elif kind == "corrupt":
            # Same hole, different cause: the row exists but cannot be read.
            report(f"{name(key[1])}: corrupt table row", key[1], "corrupt_row")
            partial = True
        elif kind == "subsystem":
            report(f"{name(key[1])}: subsystem not simulated", key[1],
                   "subsystem")
        elif kind == "buff":
            # A buff id is not a type id, so there is nothing to re-resolve.
            report(f"unknown warfare buff {key[1]}", 0,
                   f"unknown_buff:{key[1]}")
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
                report(f"{name(type_id)}: {label}", type_id,
                       f"special_effect:{effect_id}")
                partial = True
                continue
            if item is not None and item.category_id == fit_sim.CATEGORY_SHIP \
                    and _effect_has_modifiers(effect_id):
                notes.append(f"{name(type_id)}: effect {effect_id} "
                             f"matched nothing on this fit")
                continue
            report(f"{name(type_id)}: effect {effect_id}", type_id,
                   f"effect:{effect_id}")
            if _is_weapon_like(item) or _is_tank_module(item):
                partial = True
        else:
            # An unrecognised key KIND -- a future engine records something
            # this layer has never heard of.  Rendered field by field so the
            # readout says what it knows instead of printing a raw tuple.
            rest = ", ".join(str(part) for part in key[1:])
            report(f"{kind}: {rest}", 0, f"unknown_key:{kind}")
    return lines, items, notes, partial


# ===========================================================================
# derivation
# ===========================================================================

def derive(fit, profile: DamageProfile = OMNI, *, links: str = TIER_NONE,
           disciplines: Sequence[str] = (),
           links_unavailable: Sequence[str] = (),
           name_of: Callable[[int], str] | None = None,
           ammo: str = AMMO_AS_FITTED,
           ammo_assumed: Sequence[tuple] = (),
           ammo_unavailable: Sequence[int] = ()) -> FitStats:
    """Read DPS / volley / range / EHP off an ALREADY EVALUATED fit.

    ``fit`` must have been through :func:`fit_sim.evaluate` -- this function
    only reads attributes, it never applies a modifier.  ``name_of`` resolves
    type ids for the readout (consumers pass ``type_catalog.resolve_name``);
    without one every name reads ``type <id>``.  Unlike :func:`simulate` it
    keeps a default, because nothing here is cached: a ``derive`` result is
    never shared with a caller that wanted different names.

    ``ammo`` / ``ammo_assumed`` / ``ammo_unavailable`` are the ammo policy's
    REPORT, not an instruction: by the time a fit reaches here its charges are
    already whatever they are going to be.  The default says ``as_fitted``,
    which is the truth for every caller that evaluated a fit itself.

    ``disciplines`` is what was ACTUALLY applied; ``links_unavailable`` names
    the disciplines that were asked for and could not be modelled (the tier's
    preset no longer resolves in this SDE).  Each of those is reported and
    makes the result ``partial``: a link tier that silently applied nothing
    would read as "your fit gains nothing from links", which is a different --
    and wrong -- statement.

    Never raises for fit data: an empty, weaponless or entirely unknown fit
    yields zeros plus an ``unmodeled`` list.
    """
    name = _namer(name_of)
    dps_turret, dps_missile, weapon_volley, weapon_rows, missing = \
        _weapon_stats(fit, name)
    dps_drone, drone_volley, drone_rows = _drone_stats(fit, name)

    lines, items, notes, partial = _unmodeled_entries(fit, name)
    if not fit.converged:
        # Rule 8 cut the fixed-point loop short, so every number below is the
        # best MAX_PASSES could do rather than a settled value. That is a
        # WARNING, not a gap: nothing is missing from the model, the arithmetic
        # simply has not stopped moving, so it never sets ``partial``.
        notes.append(f"evaluation hit the pass cap ({fit_sim.MAX_PASSES}) "
                     "— numbers may be off")
    if links_unavailable:
        # The links the caller asked for and did not get -- an honest "this
        # number is missing a boost" rather than a silently unboosted fit.
        lines = [f"command links ({links}/{discipline}): no buffs from this "
                 "preset" for discipline in links_unavailable] + lines
        items = [(0, "links_unavailable")
                 for _discipline in links_unavailable] + items
        partial = True
    if ammo_unavailable:
        # The ammo policy was asked for a charge and found none of the right
        # group and size.  NOT partial on its own: the weapon kept whatever the
        # pilot loaded, so the number is as right as ``as_fitted`` would be --
        # and if it loaded NOTHING, the "no charge loaded" line below is the
        # one that says the DPS is missing.
        lines = [f"{name(type_id)}: no close-range ammo found"
                 for type_id in ammo_unavailable] + lines
        items = [(int(type_id), "no_ammo_candidate")
                 for type_id in ammo_unavailable] + items
    if missing:
        # A weapon with no ammo is the loudest hole there is: first in the list.
        lines = [f"{n}: no charge loaded" for _tid, n in missing] + lines
        items = [(tid, "no_charge") for tid, _n in missing] + items
        partial = True
    lines, items = _fold_repeats(lines, items)

    layers = _layer_stats(fit.ship, profile)
    resists = tuple((layer, layers[layer][2]) for layer, _hp, _res
                    in LAYER_SPECS)
    try:
        build = dogma_data.sde_build()
    except dogma_data.DogmaUnavailable:                   # pragma: no cover
        build = 0

    # The owner's drone rule: drones count toward the HEADLINE only on a ship
    # that is primarily a drone ship.  Strictly more than half, so a fit whose
    # drones are exactly half of it (a Gila-shaped 50/50) reports the gun
    # number and says drones are excluded -- and a fit with no damage at all is
    # never "primarily" anything.  The breakdown reports drones either way.
    dps_weapons = dps_turret + dps_missile
    dps_raw_total = dps_weapons + dps_drone
    volley_raw = weapon_volley + drone_volley
    drones_counted = dps_drone > 0.5 * dps_raw_total

    return FitStats(
        dps_total=dps_raw_total if drones_counted else dps_weapons,
        dps_turret=dps_turret,
        dps_missile=dps_missile,
        dps_drone=dps_drone,
        volley=volley_raw if drones_counted else weapon_volley,
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
        unmodeled_items=tuple(items),
        notes=tuple(notes),
        sde_build=build,
        partial=partial,
        dps_raw_total=dps_raw_total,
        volley_raw=volley_raw,
        drones_counted=drones_counted,
        ammo=ammo,
        ammo_assumed=tuple((int(weapon_id), int(charge_id), str(charge_name))
                           for weapon_id, charge_id, charge_name
                           in ammo_assumed),
    )


# ===========================================================================
# the facade + its bounded LRU
# ===========================================================================

#: ``(content hash, links, disciplines, profile, ammo) -> FitStats``, oldest
#: first.
#: Bounded at :data:`CACHE_SIZE`; ``_cache_lock`` guards it because several
#: worker threads (the fit readout and the fleet aggregator) share it.
_cache: OrderedDict = OrderedDict()
_cache_lock = threading.Lock()

class _CacheState:
    """The cache's generation number, and nothing else.

    A slotted holder rather than a module-level ``int`` that ``clear_cache``
    rebinds with ``global``: the purity guard
    (``tests/test_fit_sim_purity.py``) refuses undeclared global rebinds in
    these modules, and rightly -- the failure it hunts is a pure module that
    quietly starts remembering things.  One int behind ``__slots__`` is
    bounded by construction and cannot grow into that.
    """

    __slots__ = ("generation",)

    def __init__(self) -> None:
        self.generation = 0


#: Bumped by :func:`clear_cache`.  A ``simulate`` call that started before the
#: table was swapped must not file its stale result afterwards -- it snapshots
#: this before computing and drops the INSERT (never the answer it already
#: gave the caller) when the number moved underneath it.
_cache_state = _CacheState()


def clear_cache() -> None:
    """Drop every memoised result.  Call after a table reload."""
    with _cache_lock:
        _cache.clear()
        _cache_state.generation += 1


def simulate(parsed: ParsedFit, *, name_of: Callable[[int], str] | None,
             links: str = TIER_NONE,
             disciplines: str = fit_sim_links.MODE_AUTO,
             profile: DamageProfile = OMNI,
             ammo: str = AMMO_BEST_CLOSE) -> FitStats:
    """Simulate one parsed fit at All-V skills.  The single public entry point.

    Cached on ``(fit_content_hash(parsed), links, disciplines, profile,
    ammo)``: the content hash is order-independent, so re-sorting a fit's
    modules is a cache HIT.

    ``ammo`` is one of :data:`AMMO_MODES`.  The default ``"best_close"`` is the
    owner's rule -- every weapon group is loaded with the highest-DPS
    close-range non-Tech-II charge the table offers it, chosen by SIMULATING
    the top :data:`AMMO_CANDIDATES` and keeping the winner, so a hull's own
    damage-type bonus picks its ammunition.  ``"as_fitted"`` keeps whatever the
    EFT loaded.  Either way :attr:`FitStats.ammo` records which, and
    ``best_close`` names every assumption in :attr:`FitStats.ammo_assumed`.

    ``name_of`` is REQUIRED and deliberately NOT part of the key.  The strings
    it renders are cached with the numbers, so whichever caller misses first
    decides the names every later caller sees; making it required is what stops
    that being an accident.  **Every app consumer passes the one
    ``TypeCatalog.resolve_name``**, which makes the shared strings correct by
    construction.  ``name_of=None`` is a legitimate explicit choice (names read
    ``type <id>``) -- but a caller that mixes it with a real resolver on the
    same fit gets whichever came first, so tests that care must
    :func:`clear_cache` between them.  For a consumer that has to re-resolve a
    name itself, :attr:`FitStats.unmodeled_items` carries the type ids and
    :attr:`WeaponRange.type_id` the weapons'.

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
    clean re-evaluation, not an accumulation on top of the first.  ``ammo=
    "best_close"`` adds at most :data:`AMMO_CANDIDATES` evaluates per weapon
    GROUP (not per gun) -- and none at all for a fit with no weapons, or one
    whose weapon has a single candidate.  The ammo search runs ONCE, on the
    bare fit, before any link buff: no v1 dbuff touches a weapon attribute, so
    the choice cannot depend on the tier.

    Raises :class:`ValueError` for an unknown tier, discipline mode or ammo
    mode, and
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
    if ammo not in AMMO_MODES:
        raise ValueError(f"unknown ammo mode {ammo!r}; expected one of "
                         f"{list(AMMO_MODES)}")
    if not dogma_data.is_loaded():
        raise dogma_data.DogmaUnavailable(
            "no dogma table loaded -- call dogma_data.load() from a worker "
            "thread and honour a False result")

    key = (fit_content_hash(parsed), links, disciplines, profile, ammo)
    with _cache_lock:
        generation = _cache_state.generation
        hit = _cache.get(key)
        if hit is not None:
            _cache.move_to_end(key)
            return hit

    if ammo == AMMO_BEST_CLOSE:
        charges, ammo_unavailable, ammo_assumed = _choose_ammo(
            parsed, _namer(name_of))
        effective = _reloaded(parsed, charges)
    else:
        ammo_unavailable, ammo_assumed = (), ()
        effective = parsed

    fit = fit_sim.build_fit(effective)
    unavailable: tuple = ()
    if links == TIER_NONE:
        # No booster, no discipline: the mode is validated but never resolved,
        # so the no-links tier costs exactly one evaluation.
        applied: tuple = ()
        buffs: tuple = ()
    else:
        if disciplines == fit_sim_links.MODE_AUTO:
            fit_sim.evaluate(fit)               # the receiver at rest
        requested = fit_sim_links.choose_disciplines(fit, disciplines)
        # Per discipline rather than through ``buffs_for``, because a booster
        # that broadcasts NOTHING has to be told apart from one that was never
        # asked for: a preset id this SDE no longer carries yields no buffs,
        # and applying nothing while still claiming the tier would be a lie
        # the readout has no way to see.
        applied_list, unavailable_list, collected = [], [], []
        for discipline in requested:
            broadcast = fit_sim_links.booster_buffs(links, discipline)
            if broadcast:
                applied_list.append(discipline)
                collected.extend(broadcast)
            else:
                unavailable_list.append(discipline)
        applied = tuple(applied_list)
        unavailable = tuple(unavailable_list)
        buffs = tuple(collected)

    stats = derive(fit_sim.evaluate(fit, buffs), profile,
                   links=links, disciplines=applied,
                   links_unavailable=unavailable, name_of=name_of,
                   ammo=ammo, ammo_assumed=ammo_assumed,
                   ammo_unavailable=ammo_unavailable)

    with _cache_lock:
        # A ``clear_cache`` that landed WHILE this was computing means the
        # table (or the names) changed underneath it: the caller still gets the
        # answer it asked for, but nobody else inherits it.
        if _cache_state.generation == generation:
            _cache[key] = stats
            _cache.move_to_end(key)
            while len(_cache) > CACHE_SIZE:
                _cache.popitem(last=False)
    return stats
