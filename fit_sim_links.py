"""Command-link tiers: derive warfare buffs by evaluating a canonical booster.

Pure stdlib, no Tk, no network, no ``fc_gui``.  Layering (imports flow only
downward)::

    dogma_data <- fit_sim <- fit_sim_links <- fit_sim_stats <- {panel, fleet}

The point of this module is that it hand-codes **no strengths**.  A tier is a
canonical booster -- a hull, a Command Burst module per discipline, the three
charges of that discipline, and (at ``max``) the matching Mindlink -- and the
buff values come out of :mod:`fit_sim` evaluating that booster exactly like any
other fit.  The only hand-curated data here is :data:`TIER_PRESETS`: WHICH type
ids make up each tier.  Everything downstream of those ids is dogma.

How a burst's strength is assembled (read off the shipped table, SDE build
3494416 -- spec Appendix A.6 / A.7)::

    module warfareBuff{N}Value      T1 1.0        T2 1.25       (base attribute)
      postMul  by the CHARGE's warfareBuff{N}Multiplier (+-8), effect 6737,
               domain otherID -- which ALSO postAssigns the charge's
               warfareBuff{N}ID onto the module; that id names the dbuff the
               receiver applies
      postPercent by the SPECIALIST skill of the discipline -- Shield Command
               Specialist (3351) or Armored Command Specialist (11569), the
               SDE's own names; attribute 2572 = 10 per level -> +50 % at V
      postPercent by the hull's eliteBonusCommandShips3 (attribute 1924 = 3.0,
               preMul'd by Command Ships level -> +15 % at V) -- Command Ships
               only, which is exactly what separates ``bonused`` from ``basic``
      postPercent by the Mindlink's mindlinkBonus (attribute 884 = 25.0)

None of those steps stack-penalise (the sources are a charge, a skill, a hull
and an implant -- never a module or drone), so they simply multiply.  Measured
Shield Harmonizing strengths at All V: basic 12 %, bonused 17.25 %, max
21.5625 % (1.25 x 8 x 1.5 x 1.15 x 1.25).

**v1 scope.**  Only four dbuffs move a stat the v1 readout shows -- 10 shield
resistance, 12 shield HP, 13 armor resistance, 15 armor HP -- so only the
:data:`DISCIPLINES` ``shield`` and ``armor`` have presets.  Skirmish and
Information bursts touch signature radius, agility, speed, scan resolution,
targeting range and EWAR, none of which the v1 stat set carries, so applying
them would change no number while implying the readout knew something it does
not (spec 4).  The other two charges per discipline (Active Shielding / Rapid
Repair, dbuffs 11 and 14) ARE still derived and returned: they cost nothing,
they are what a real booster broadcasts, and the receiver's engine simply never
reads the attributes they move.

**Never loads anything.**  ``dogma_data.load()`` is the caller's business, on a
worker thread; the ``none`` tier answers without touching the table at all, and
every other tier raises :class:`dogma_data.DogmaUnavailable` when none is
installed.
"""

from __future__ import annotations

import functools
import threading
from types import MappingProxyType
from typing import NamedTuple, Sequence

import dogma_data
import fit_sim
from fit_models import ParsedFit, ParsedModule

# ===========================================================================
# the vocabulary
# ===========================================================================

#: The no-links tier: no booster, no buff, no table read.
TIER_NONE = "none"

#: Link tiers, weakest first.
TIERS = (TIER_NONE, "basic", "bonused", "max")

#: The disciplines with a preset.  v1 has exactly the two whose dbuffs move a
#: number the readout shows (see the module docstring).
DISCIPLINES = ("shield", "armor")

MODE_AUTO = "auto"
MODE_BOTH = "both"
MODE_NONE = "none"

#: Accepted ``disciplines`` MODE strings.  ``auto`` picks the fit's dominant
#: tank layer; ``none`` is the explicit "apply nothing", which is what makes a
#: non-``none`` tier with no discipline expressible -- and is how the identity
#: ``simulate(links="none")`` == ``simulate(links=<tier>, disciplines="none")``
#: is stated.
DISCIPLINE_MODES = (MODE_AUTO, "shield", "armor", MODE_BOTH, MODE_NONE)

# ===========================================================================
# type ids (spec Appendix A.6, re-verified against the shipped table)
# ===========================================================================
# Every id below is asserted to exist by test_fit_sim_links.py's real-table
# golden test.  They are named constants rather than literals in the table
# below so a wrong id reads as a wrong NAME.

#: Unbonused hull for ``basic``: a Combat Battlecruiser (group 419), which is in
#: the burst module's ``canFitShipGroup`` list (A.6) and carries no
#: ``eliteBonusCommandShips3``.  Myrmidon, Ferox, Brutix, Cyclone, Prophecy,
#: Harbinger and Hurricane are equally valid; the Drake is the familiar one.
DRAKE = 24698

#: Command Ships (group 540) whose ``eliteBonusCommandShips3`` reaches the burst
#: of their discipline: effect 5573 filters on skill 3350 (Shield Command),
#: effect 5572 on 20494 (Armored Command) -- exactly the skills the matching
#: burst module requires.  A Vulture would NOT bonus an armor burst, which is
#: why the hull is chosen per discipline.
VULTURE = 22446
DAMNATION = 22474

SHIELD_COMMAND_BURST_I = 42529
SHIELD_COMMAND_BURST_II = 43555
ARMOR_COMMAND_BURST_I = 42526
ARMOR_COMMAND_BURST_II = 43552

#: The three shield charges: dbuff 10 (resistance), 11 (booster duration and
#: capacitor need), 12 (shield HP).
SHIELD_HARMONIZING_CHARGE = 42695
ACTIVE_SHIELDING_CHARGE = 42694
SHIELD_EXTENSION_CHARGE = 42696
SHIELD_CHARGES = (SHIELD_HARMONIZING_CHARGE, ACTIVE_SHIELDING_CHARGE,
                  SHIELD_EXTENSION_CHARGE)

#: The three armor charges: dbuff 13 (resistance), 14 (repairer duration and
#: capacitor need), 15 (armor HP).
ARMOR_ENERGIZING_CHARGE = 42832
RAPID_REPAIR_CHARGE = 42833
ARMOR_REINFORCEMENT_CHARGE = 42834
ARMOR_CHARGES = (ARMOR_ENERGIZING_CHARGE, RAPID_REPAIR_CHARGE,
                 ARMOR_REINFORCEMENT_CHARGE)

SHIELD_COMMAND_MINDLINK = 21888
ARMORED_COMMAND_MINDLINK = 13209


class BoosterPreset(NamedTuple):
    """One tier's canonical booster, discipline by discipline.

    EVERY field is keyed by discipline, the hull included: ``bonused`` and
    ``max`` need the Command Ship whose bonus names that discipline's skill
    (Vulture for shield, Damnation for armor), so the plan's sketch of a single
    ``hull_type_id`` would have given the armor tiers a hull that bonuses
    nothing -- a silent 15 % under-count, not an error.

    Every field is a ``MappingProxyType``: a preset is shared -- one charge
    map serves all three tiers, and every caller reads the SAME
    :data:`TIER_PRESETS` entry -- so an in-place edit anywhere would silently
    re-point every other reader's booster.  Read-only by construction beats
    read-only by convention.
    """

    hull_type_id: MappingProxyType            # discipline -> hull type id
    burst_type_id: MappingProxyType           # discipline -> burst module id
    charge_type_ids: MappingProxyType         # discipline -> 3 charge ids
    mindlink_type_id: MappingProxyType        # discipline -> implant id or None


#: Shared by every tier: the charge set is a property of the discipline, not of
#: how bonused the booster is.  Immutable, so one object safely serves all
#: three tiers.
_CHARGES = MappingProxyType({"shield": SHIELD_CHARGES, "armor": ARMOR_CHARGES})
_NO_MINDLINK = MappingProxyType({"shield": None, "armor": None})

#: ``tier -> BoosterPreset``.  ``none`` is deliberately ABSENT: it has no
#: booster, and :func:`booster_buffs` answers it without ever reading a table.
TIER_PRESETS = {
    "basic": BoosterPreset(
        hull_type_id=MappingProxyType({"shield": DRAKE, "armor": DRAKE}),
        burst_type_id=MappingProxyType({"shield": SHIELD_COMMAND_BURST_I,
                                        "armor": ARMOR_COMMAND_BURST_I}),
        charge_type_ids=_CHARGES,
        mindlink_type_id=_NO_MINDLINK),
    "bonused": BoosterPreset(
        hull_type_id=MappingProxyType({"shield": VULTURE,
                                       "armor": DAMNATION}),
        burst_type_id=MappingProxyType({"shield": SHIELD_COMMAND_BURST_II,
                                        "armor": ARMOR_COMMAND_BURST_II}),
        charge_type_ids=_CHARGES,
        mindlink_type_id=_NO_MINDLINK),
    "max": BoosterPreset(
        hull_type_id=MappingProxyType({"shield": VULTURE,
                                       "armor": DAMNATION}),
        burst_type_id=MappingProxyType({"shield": SHIELD_COMMAND_BURST_II,
                                        "armor": ARMOR_COMMAND_BURST_II}),
        charge_type_ids=_CHARGES,
        mindlink_type_id=MappingProxyType(
            {"shield": SHIELD_COMMAND_MINDLINK,
             "armor": ARMORED_COMMAND_MINDLINK})),
}

# ===========================================================================
# attribute ids
# ===========================================================================

#: ``(warfareBuff{N}ID, warfareBuff{N}Value)`` for every buff slot a Command
#: Burst can carry.  The shipped table uses 2468/2469, 2470/2471, 2472/2473 and
#: **2536/2537** -- effect 6737 writes the 2536 pair as its fourth, NOT
#: 2474/2475.  2474/2475 is listed anyway so a table that does populate it is
#: read rather than silently dropped; an unused slot's ID attribute is 0.
WARFARE_BUFF_ATTRS = ((2468, 2469), (2470, 2471), (2472, 2473),
                      (2474, 2475), (2536, 2537))

#: Raw fitted HP of the two tank layers, for the ``auto`` discipline choice.
ATTR_SHIELD_CAPACITY = 263
ATTR_ARMOR_HP = 265


# ===========================================================================
# discipline choice
# ===========================================================================

def choose_disciplines(fit, mode: str) -> tuple[str, ...]:
    """Which disciplines a receiver should be boosted in.

    ``mode`` is one of :data:`DISCIPLINE_MODES`.  ``auto`` compares the fit's
    RAW fitted HP -- shield capacity (263) against armor HP (265) -- and picks
    the larger, which is the tank the pilot actually built; a tie (including
    the both-zero of an unknown hull) resolves to shield, so the answer is
    deterministic rather than a coin toss on float equality.

    ``fit`` must be an already-EVALUATED :class:`fit_sim.FitState` for ``auto``
    to see plates and extenders (an unevaluated one still answers, off base
    hull HP -- it is just the wrong question).  The other modes never read it.

    A mode outside :data:`DISCIPLINE_MODES` is caller misuse, not fit data, so
    it raises :class:`ValueError` -- the engine's never-raise rule covers DATA.
    """
    if mode == MODE_AUTO:
        shield = fit_sim.attr(fit.ship, ATTR_SHIELD_CAPACITY)
        armor = fit_sim.attr(fit.ship, ATTR_ARMOR_HP)
        return ("armor",) if armor > shield else ("shield",)
    if mode == MODE_BOTH:
        return DISCIPLINES
    if mode == MODE_NONE:
        return ()
    if mode in DISCIPLINES:
        return (mode,)
    raise ValueError(f"unknown discipline mode {mode!r}; expected one of "
                     f"{list(DISCIPLINE_MODES)}")


# ===========================================================================
# buff derivation
# ===========================================================================

#: Identity of the table the memoised boosters were derived from.  The build
#: number alone cannot tell two tables apart (every hand-built test table
#: declares the same one), so the table OBJECT is held for an ``is`` test --
#: read for identity ONLY, never indexed, never written.  Holding the strong
#: reference is what makes that test sound: a dropped table's id could
#: otherwise be recycled by the next one.  Bounded: two scalars, the same shape
#: ``fit_sim._skill_prototypes`` uses for the same reason.
_CACHE_TABLE = None
_CACHE_BUILD = None
_CACHE_LOCK = threading.Lock()


def clear_cache() -> None:
    """Drop every memoised booster.  Call after a table reload."""
    global _CACHE_TABLE, _CACHE_BUILD
    with _CACHE_LOCK:
        _derive_buffs.cache_clear()
        _CACHE_TABLE = None
        _CACHE_BUILD = None


def _sync_cache_to_table() -> None:
    """Invalidate the memo when a DIFFERENT table has been installed.

    :func:`_derive_buffs` is keyed on ``(tier, discipline)`` alone, so without
    this a booster derived from the shipped table would be handed straight back
    after a test seeded a mini one, and vice versa.

    Raises :class:`dogma_data.DogmaUnavailable` when nothing is loaded -- which
    is the right answer for every tier that needs a booster.
    """
    global _CACHE_TABLE, _CACHE_BUILD
    build = dogma_data.sde_build()
    table = getattr(dogma_data, "_table", None)
    with _CACHE_LOCK:
        if _CACHE_TABLE is not table or _CACHE_BUILD != build:
            _derive_buffs.cache_clear()
            _CACHE_TABLE = table
            _CACHE_BUILD = build


def _booster_fit(tier: str, discipline: str):
    """The canonical booster for one ``(tier, discipline)``, evaluated at All V.

    Three burst modules of the preset's type, one per charge: a real booster
    cycles all three, and one module per charge is what gives each charge its
    own ``warfareBuff`` slot to write into (effect 6737 assigns to the module
    that holds it).
    """
    preset = TIER_PRESETS[tier]
    burst = preset.burst_type_id[discipline]
    modules = [ParsedModule(type_id=burst, name="Command Burst", slot="high",
                            charge_type_id=charge, charge_name="Charge")
               for charge in preset.charge_type_ids[discipline]]
    parsed = ParsedFit(ship_type_id=preset.hull_type_id[discipline],
                       ship_name="Canonical booster", modules=modules,
                       drones=[], cargo=[], subsystems=[])
    mindlink = preset.mindlink_type_id[discipline]
    implants = () if mindlink is None else (mindlink,)
    return fit_sim.evaluate(fit_sim.build_fit(parsed, implants=implants))


@functools.lru_cache(maxsize=8)
def _derive_buffs(tier: str, discipline: str) -> tuple:
    """Evaluate the booster and read its buffs off the burst modules.

    Bounded at 8 entries: the whole product of tiers that have a preset (3) and
    disciplines (2), with room to spare.  :func:`_sync_cache_to_table` clears it
    whenever the installed table changes.
    """
    fit = _booster_fit(tier, discipline)
    buffs = []
    for burst in fit.modules:
        for id_attr, value_attr in WARFARE_BUFF_ATTRS:
            buff_id = fit_sim.attr(burst, id_attr)
            if not buff_id:
                continue
            buffs.append(fit_sim.Buff(int(buff_id),
                                      fit_sim.attr(burst, value_attr)))
    # Sorted so callers (and tests) see a stable order regardless of which
    # module the engine happened to fold first.
    return tuple(sorted(buffs))


def _check_arguments(tier: str, discipline: str) -> None:
    """Reject a caller's typo before it becomes a silently empty booster.

    A bad tier or discipline is caller misuse, not fit data, so it raises --
    the engine's never-raise rule covers DATA.  Shared by every public
    ``(tier, discipline)`` entry point so they cannot drift apart.
    """
    if tier not in TIERS:
        raise ValueError(f"unknown link tier {tier!r}; expected one of "
                         f"{list(TIERS)}")
    if discipline not in DISCIPLINES:
        raise ValueError(f"unknown discipline {discipline!r}; expected one of "
                         f"{list(DISCIPLINES)}")


def booster_buffs(tier: str, discipline: str) -> tuple:
    """The :class:`fit_sim.Buff` tuple one tier broadcasts in one discipline.

    ``none`` answers ``()`` without touching the table; every other tier needs
    one and raises :class:`dogma_data.DogmaUnavailable` when none is loaded.
    This never calls ``dogma_data.load()`` -- that multi-megabyte decode belongs
    to the caller's worker thread.  Memoised: a booster is the same for every
    receiving fit.

    An EMPTY tuple from a real tier is meaningful, not an error: it means the
    canonical booster found nothing to broadcast -- a preset type this SDE no
    longer carries (see :func:`preset_available`), or a hull that no longer
    bonuses the burst.  Callers must report that rather than quietly applying
    nothing; ``fit_sim_stats.simulate`` does.
    """
    _check_arguments(tier, discipline)
    if tier == TIER_NONE:
        return ()
    _sync_cache_to_table()
    return _derive_buffs(tier, discipline)


def preset_type_ids(tier: str, discipline: str) -> tuple:
    """Every type id one ``(tier, discipline)`` booster is made of.

    ``none`` has no booster, so it answers ``()``.  Order is hull, burst, the
    three charges, then the Mindlink when the tier wears one.
    """
    _check_arguments(tier, discipline)
    if tier == TIER_NONE:
        return ()
    preset = TIER_PRESETS[tier]
    ids = [preset.hull_type_id[discipline], preset.burst_type_id[discipline]]
    ids.extend(preset.charge_type_ids[discipline])
    mindlink = preset.mindlink_type_id[discipline]
    if mindlink is not None:
        ids.append(mindlink)
    return tuple(ids)


def preset_available(tier: str, discipline: str) -> bool:
    """Whether the LOADED table still carries every type this preset names.

    A curated id is the one thing in this feature that data cannot check for
    itself: CCP can rename, re-id or remove a hull, a burst or a charge, and a
    preset that no longer resolves would otherwise model a booster with nothing
    in it and hand back a silent zero.  Consumers use this to say so out loud
    (``fit_sim_stats.simulate`` reports it as ``links_unavailable``) or to grey
    a tier out before offering it.

    ``none`` is always available -- it needs no preset and reads no table.
    Every other tier reads the table, so this raises
    :class:`dogma_data.DogmaUnavailable` when none is loaded, exactly like
    :func:`booster_buffs`.
    """
    ids = preset_type_ids(tier, discipline)
    if not ids:
        return True
    return all(dogma_data.has_type(type_id) for type_id in ids)


def buffs_for(tier: str, disciplines: Sequence[str]) -> tuple:
    """Every buff ``tier`` broadcasts across ``disciplines``, concatenated.

    ``none`` -- or an empty discipline sequence -- yields ``()``, which is what
    makes the no-links tier free: :func:`fit_sim.evaluate` with no buffs plans
    no buff contributions at all.  Duplicate disciplines are NOT deduplicated;
    the engine aggregates per buff id anyway, and ``Maximum``/``Minimum`` of a
    value with itself is that value.
    """
    if tier not in TIERS:
        raise ValueError(f"unknown link tier {tier!r}; expected one of "
                         f"{list(TIERS)}")
    if tier == TIER_NONE:
        return ()
    buffs: list = []
    for discipline in disciplines:
        buffs.extend(booster_buffs(tier, discipline))
    return tuple(buffs)
