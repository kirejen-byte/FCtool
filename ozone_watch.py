"""'Watch my ozone' — cyno ozone cost / cargo / latch engine. PURE.

Design: ``docs/superpowers/specs/2026-09-12-ozone-watch-design.md`` (§3 "O1" is
this module's contract; §1 carries the verified dogma facts this file encodes).

The feature warns a pilot who undocks, logs in, or lights a cyno in a hull with
a Cynosural Field Generator fitted and fewer than ``min_activations`` lights of
Liquid Ozone aboard. This module is the whole of the decision: the ESI reads,
the toast and the Tk plumbing live in ``fc_gui``.

**Layering.** Standard library plus ``dogma_data`` (the lazy, offline reader for
the bundled ``fit_dogma.json.gz``) and ``app_log``. No Tk, no ``requests``, no
``esi_auth``, no ``fc_gui`` — pinned by an AST guard in
``tests/test_ozone_watch.py``. Nothing here touches the network and nothing
loads at import time: the dogma table is decoded on first cost question, on
whatever worker thread asks (``dogma_data.load()`` is lock-guarded and
idempotent).

**Every public function is TOTAL.** A missing table, an unknown type, a hostile
asset row, a string where an int belongs — all degrade to the documented
"nothing to say" answer (``None`` / an empty ``ShipCargo`` / ``CLEAR``). The
error direction is the implant reminder's: a missed warning is cheap, a
spurious nag is what gets a feature switched off. So we never GUESS a cost —
an unknown generator answers ``None`` and the state machine stays quiet.

The cost model (measured against the shipped table, SDE build 3494416)
-----------------------------------------------------------------------
A generator's Liquid Ozone consumption is attribute **714**
(``consumptionQuantity``). Two multiplicative reductions apply, and the SDE
expresses both as *percentage* values carried in attribute **1296**, applied by
effect **3526** (``LocationRequiredSkillModifier(714 <- 1296, postPercent,
requiredSkill 21603)``):

* the **skill**: type 21603 (Cynosural Field Theory) carries ``1296 = -10``,
  which is **per level** — the two-step rule. At level L the factor is
  ``1 + L * (-10) / 100`` (V ⇒ ×0.5, 0 ⇒ ×1.0).
* the **hull**: a hull that carries ``1296`` *and* an effect whose modifier row
  targets 714 through 1296 **keyed on skill 21603**. Force Recons carry -80,
  Ventures -50. The skill-id filter is load-bearing: other hulls modify 714
  through a *different* required skill (the Rorqual/Orca industrial-cyno
  family), and those must not leak into a combat cyno's cost.

There is **no Black Ops entry** in the SDE — a covert cyno on a Sin costs the
same as on any unbonused hull. That is the data, not an omission.

The two factors do not stack-penalise (ship and skill sources are exempt), so
the result is ``ceil(base x skill_factor x hull_factor)``. Golden rows, all
verified against the bundled table::

    (21096 Cyno I,   11963 Rapier, V) -> 50     500 x 0.5 x 0.2
    (21096 Cyno I,   12013 Broadsword, V) -> 250   (no hull bonus)
    (28646 Covert,   11963 Rapier, V) -> 5      50 x 0.5 x 0.2
    (28646 Covert, 22436 Widow,   V) -> 25     (Black Ops: NO bonus)
    (52694 Indy,     32880 Venture, V) -> 200   800 x 0.5 x 0.5
    (21096 Cyno I,   11963 Rapier, 0) -> 100    500 x 1.0 x 0.2

Liquid Ozone is type **16273**. The bundled table does not model it (it is a
charge nothing in the fit sim modifies) and ``fit_types.json`` carries no name
for it either, so the id is hardcoded here and the display string is ours.
"""

from __future__ import annotations

import math
import threading
from dataclasses import dataclass, field
from typing import NamedTuple

import dogma_data
from app_log import get_logger

log = get_logger(__name__)


# ── config ───────────────────────────────────────────────────────────────────

#: Shape + defaults of ``config['ozone_watch']``. Mirrored in
#: ``default_config.DEFAULT_CONFIG``; kept here too so the engine self-heals a
#: partially-written block (house per-key defaulting, never a deep merge).
DEFAULTS = {
    "enabled": True,             # MASTER GATE — explicit False = fully inert, zero ESI
    "min_activations": 4,        # warn below this many lights of ozone
    "toast_seconds": 12.0,       # auto-dismiss hold before the fade
    "asset_ttl_s": 600,          # per-ship assets cache (the Characters tab's precedent)
    "assumed_skill_level": 5,    # ESI skills are not authorised — see the spec's decision 1
    "disabled_chars": [],        # lowercased char keys that are never warned
}

#: Hard floor/ceiling on the toast hold so a corrupt config can neither flash
#: the toast for 0 s nor leave it pinned over the client forever.
TOAST_SECONDS_MIN = 3.0
TOAST_SECONDS_MAX = 60.0

#: ``min_activations`` bounds. 0 is a legitimate "never warn" reading and is
#: preserved rather than defaulted back — the ceiling only stops a typo from
#: turning every undock into a toast forever.
MIN_ACTIVATIONS_MAX = 100

#: Skill levels are 0..5. A hostile value clamps rather than raising.
SKILL_LEVEL_MAX = 5

#: Bounds on the assets cache TTL (seconds). The floor keeps a corrupt config
#: from hammering the (expensive, fully-paginated) assets endpoint.
ASSET_TTL_MIN = 60
ASSET_TTL_MAX = 3600


def is_enabled(block) -> bool:
    """Is the watch switched on, given a RAW ``config['ozone_watch']``?

    **The single answer to the master-gate question** — the Characters-tab tick,
    the poller hook and ``normalize_config`` all route through here, so the UI
    and the engine can never disagree about whether the feature is on. Two
    independent implementations of one predicate is the bug that shipped a
    ticked box over a dead reminder in ``implant_reminder``; one is the fix.

    Absent / ``None`` / malformed (a string, a list, an int) and a dict with no
    ``enabled`` key all inherit ``DEFAULTS['enabled']``; only an explicit,
    falsy ``enabled`` turns it off. Never raises.

    Cheap by requirement: this runs on the ESI poller thread for every
    character on every poll, and while the feature is off it is the whole of
    the work done."""
    if not isinstance(block, dict):
        return bool(DEFAULTS["enabled"])
    return bool(block.get("enabled", DEFAULTS["enabled"]))


def _clamped_int(value, fallback: int, low: int, high: int) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return int(fallback)
    if isinstance(value, bool):          # True is not "1 activation"
        return int(fallback)
    return max(low, min(n, high))


def normalize_config(raw) -> dict:
    """Coerce ``config['ozone_watch']`` into a fully-populated, sane dict.

    Never raises and never returns a partial shape: a missing block, a wrong
    type, or a hostile value all degrade to the documented default for that one
    key. Pure — the caller's dict is not mutated, and the two list values are
    fresh lists the caller owns."""
    src = raw if isinstance(raw, dict) else {}
    out = dict(DEFAULTS)
    out["disabled_chars"] = []

    # Delegated, not duplicated: is_enabled is THE master-gate predicate.
    out["enabled"] = is_enabled(src)

    try:
        secs = float(src.get("toast_seconds", DEFAULTS["toast_seconds"]))
    except (TypeError, ValueError):
        secs = float(DEFAULTS["toast_seconds"])
    out["toast_seconds"] = max(TOAST_SECONDS_MIN, min(secs, TOAST_SECONDS_MAX))

    out["min_activations"] = _clamped_int(
        src.get("min_activations"), DEFAULTS["min_activations"],
        0, MIN_ACTIVATIONS_MAX)
    out["assumed_skill_level"] = _clamped_int(
        src.get("assumed_skill_level"), DEFAULTS["assumed_skill_level"],
        0, SKILL_LEVEL_MAX)
    out["asset_ttl_s"] = _clamped_int(
        src.get("asset_ttl_s"), DEFAULTS["asset_ttl_s"],
        ASSET_TTL_MIN, ASSET_TTL_MAX)

    chars = src.get("disabled_chars")
    if isinstance(chars, (list, tuple)):
        # `c is not None` is deliberate: str(None) is "None", so the implant
        # reminder's bare `if str(c).strip()` turns a null entry in the JSON
        # into a character literally keyed "none".
        out["disabled_chars"] = [str(c).strip().lower() for c in chars
                                 if c is not None and str(c).strip()]

    return out


# ── dogma constants (SDE facts; see the module docstring) ────────────────────

#: groupID of "Cynosural Field Generator" — the three generators live here.
GENERATOR_GROUP = 658

#: Liquid Ozone. NOT modelled by the bundled table and NOT named by
#: ``fit_types.json``, so both the id and the label are ours.
LIQUID_OZONE_TYPE_ID = 16273
LIQUID_OZONE_NAME = "Liquid Ozone"

#: Cynosural Field Theory — the required skill every generator declares and the
#: one the hull bonus must be keyed on.
CYNO_SKILL_ID = 21603

ATTR_CONSUMPTION = 714            # consumptionQuantity (ozone units per light)
ATTR_CONSUMPTION_BONUS = 1296     # the PERCENTAGE reduction, per level for a skill
ATTR_DURATION = 73                # the generator's cycle time, in MILLISECONDS
EFFECT_CYNO_CONSUMPTION = 3526    # LocationRequiredSkillModifier(714 <- 1296, skill 21603)

# NOTE on reading these three through ``type_attrs``: the bundled table OMITS
# every attribute whose value equals the attribute default, and all three of
# these default to 0.0 (measured: ``attr_info(73/714/1296).default == 0.0``).
# So "absent" and "zero" are the same reading here, and an absent 714 is
# correctly treated as "no cost known" rather than "costs nothing" — which is
# why ``effective_ozone_cost`` answers None on a missing 714 instead of
# defaulting it in the way ``fit_sim.attr()`` would.

#: A generator's cycle time when the table cannot answer (the 10-minute one the
#: two full-size generators carry). Used by the cyno-lit dedupe window.
DEFAULT_CYCLE_S = 600.0

#: Used only when the table cannot answer for type 21603 (a missing or corrupt
#: ``fit_dogma.json.gz``). The SDE value, stated so a degraded table produces
#: the same number the shipped one does rather than a silently different cost.
SKILL_BONUS_PER_LEVEL = -10.0

#: The three generators, for the ``is_generator`` fallback when the table is
#: unavailable or does not carry the type.
KNOWN_GENERATOR_IDS = frozenset({21096, 28646, 52694})

#: Short display names for the toast body.
GENERATOR_SHORT = {
    21096: "Cyno",             # Cynosural Field Generator I
    28646: "Covert cyno",      # Covert Cynosural Field Generator I
    52694: "Industrial cyno",  # Industrial Cynosural Field Generator I
}

#: Fallback label for a generator id the map does not carry (a new module).
GENERATOR_SHORT_DEFAULT = "Cyno"


# ── cyno-capable hulls ───────────────────────────────────────────────────────
# The gate that decides whether an undock/login edge is worth an assets read.
#
# This is NOT a hand-picked "probably cyno-ish" list: it is the SDE's own
# fitting restriction, read off the three generators' ``canFitShipGroupNN`` and
# ``canFitShipTypeNN`` attributes (those attributes are FILTERED OUT of the
# bundled ``fit_dogma.json.gz``, so they were read from ``tools/_cache/sde.zip``
# at review time). Every hull below can actually mount a cynosural field
# generator, and every hull NOT below cannot — so a group that merely sounds
# cyno-capable (Combat Recon, Mining Barge, Exhumer, Capital Industrial,
# Industrial Command, the T1 Frigate group at large) is deliberately absent:
# 66 hulls that cannot fit any of the three modules.
#
# Two shapes are needed because the SDE restricts by group AND by individual
# type — the industrial generator names five specific hulls that live in groups
# whose other members cannot fit it (an Etana is a Logistics cruiser, a Venture
# a plain Frigate).
#
# ``ship_classes.CYNO_LOSS_GROUPS`` stays a strict SUBSET (test-pinned): this
# gate still covers every hull CynoCheck treats as a lightswitch.
CYNO_HULL_GROUP_NAMES = {
    28: "Hauler",                       # industrial cyno
    380: "Deep Space Transport",        # industrial cyno
    830: "Covert Ops",                  # covert cyno
    833: "Force Recon Ship",            # normal OR covert cyno; carries the -80 bonus
    834: "Stealth Bomber",              # covert cyno
    894: "Heavy Interdiction Cruiser",  # normal cyno
    898: "Black Ops",                   # covert cyno; NO SDE bonus (see the docstring)
    963: "Strategic Cruiser",           # covert cyno via the covert subsystem
    1202: "Blockade Runner",            # industrial cyno
}

#: Hulls the SDE names INDIVIDUALLY on a generator's ``canFitShipTypeNN``,
#: because their group is not cyno-capable as a whole. Measured, like the
#: groups, off the SDE's own fitting restrictions.
CYNO_HULL_TYPE_NAMES = {
    32790: "Etana",                     # group 832 Logistics
    42245: "Rabisu",                    # group 832 Logistics
    33697: "Prospect",                  # group 1283 Expedition Frigate
    32880: "Venture",                   # group 25 Frigate
    89648: "Venture Consortium Issue",  # group 25 Frigate
}

#: The gate itself. Frozen so no consumer can widen either half in place.
CYNO_HULL_GROUPS = frozenset(CYNO_HULL_GROUP_NAMES)
CYNO_HULL_TYPE_IDS = frozenset(CYNO_HULL_TYPE_NAMES)


def is_cyno_hull(group_id, type_id=None) -> bool:
    """Can this hull mount a cynosural field generator?

    True when the hull's GROUP is cyno-capable or the hull is one of the five
    types the SDE names individually. ``type_id`` is optional so a caller that
    only has a group can still ask — but a caller that has both should pass
    both, or it will miss a Venture (group 25 is not cyno-capable at large).

    Total: non-integers on either side answer False rather than raising."""
    try:
        if int(group_id) in CYNO_HULL_GROUPS:
            return True
    except (TypeError, ValueError):
        pass
    try:
        return int(type_id) in CYNO_HULL_TYPE_IDS
    except (TypeError, ValueError):
        return False


# ── dogma access (injectable, total) ─────────────────────────────────────────
# Every dogma question goes through one of these tiny views, so the cost
# function never has to know whether it is reading the shipped table or a
# hand-built dict in a test. Each method is total: an unknown id, a missing
# section or a malformed row answers "nothing", never an exception.


class _Modifier(NamedTuple):
    modified_attr: int
    modifying_attr: int
    skill_type_id: int | None


class _BundledView:
    """The shipped table, loaded lazily on first use (worker thread)."""

    _warned = False

    def _ready(self) -> bool:
        try:
            if dogma_data.is_loaded():
                return True
            if dogma_data.load():
                return True
        except Exception:                                   # pragma: no cover
            pass
        if not _BundledView._warned:
            _BundledView._warned = True
            log.info("[Ozone] dogma table unavailable; ozone costs cannot be "
                     "computed (the watch stays quiet)")
        return False

    def attrs(self, type_id: int) -> dict:
        if not self._ready():
            return {}
        try:
            return dogma_data.type_attrs(type_id)
        except Exception:
            return {}

    def effects(self, type_id: int) -> tuple:
        if not self._ready():
            return ()
        try:
            return dogma_data.type_effects(type_id)
        except Exception:
            return ()

    def group(self, type_id: int):
        if not self._ready():
            return None
        try:
            return dogma_data.type_group(type_id)
        except Exception:
            return None

    def modifiers(self, effect_id: int) -> tuple:
        if not self._ready():
            return ()
        try:
            eff = dogma_data.effect(effect_id)
        except Exception:
            return ()
        if eff is None:
            return ()
        return tuple(_Modifier(m.modified_attr, m.modifying_attr, m.skill_type_id)
                     for m in eff.modifiers)


class _ModuleView:
    """An injected object exposing the ``dogma_data`` accessor surface (the
    module itself, a stub, or a seeded copy). Already loaded by contract — the
    injector owns its lifecycle — so there is no lazy-load step here."""

    def __init__(self, source):
        self._src = source

    def attrs(self, type_id: int) -> dict:
        try:
            return dict(self._src.type_attrs(type_id) or {})
        except Exception:
            return {}

    def effects(self, type_id: int) -> tuple:
        try:
            return tuple(self._src.type_effects(type_id) or ())
        except Exception:
            return ()

    def group(self, type_id: int):
        try:
            return self._src.type_group(type_id)
        except Exception:
            return None

    def modifiers(self, effect_id: int) -> tuple:
        try:
            eff = self._src.effect(effect_id)
        except Exception:
            return ()
        if eff is None:
            return ()
        try:
            return tuple(_Modifier(m.modified_attr, m.modifying_attr,
                                   m.skill_type_id)
                         for m in eff.modifiers)
        except Exception:
            return ()


class _TableView:
    """A raw wire-shaped dict — ``{"types": {...}, "effects": {...}}`` in
    ``dogma_data``'s encoding. What tests hand in: it needs no seeding of the
    real module and so cannot leak a fixture table into the rest of the
    process. Ids may be str (the JSON shape) or int (the convenient shape)."""

    def __init__(self, table: dict):
        self._t = table

    def _row(self, section: str, key):
        rows = self._t.get(section)
        if not isinstance(rows, dict):
            return None
        row = rows.get(str(key))
        if row is None:
            row = rows.get(key)
        return row

    def attrs(self, type_id: int) -> dict:
        row = self._row("types", type_id)
        if not isinstance(row, dict):
            return {}
        flat = row.get("a") or ()
        out = {}
        try:
            for i in range(0, len(flat) - 1, 2):
                out[int(flat[i])] = float(flat[i + 1])
        except (TypeError, ValueError):
            return {}
        return out

    def effects(self, type_id: int) -> tuple:
        row = self._row("types", type_id)
        if not isinstance(row, dict):
            return ()
        try:
            return tuple(int(e) for e in (row.get("e") or ()))
        except (TypeError, ValueError):
            return ()

    def group(self, type_id: int):
        row = self._row("types", type_id)
        if not isinstance(row, dict):
            return None
        try:
            return int(row["g"])
        except (KeyError, TypeError, ValueError):
            return None

    def modifiers(self, effect_id: int) -> tuple:
        row = self._row("effects", effect_id)
        if not isinstance(row, dict):
            return ()
        out = []
        for m in (row.get("m") or ()):
            # [domain, func, modifiedAttr, modifyingAttr, op, skillTypeID, groupID]
            try:
                skill = m[5]
                out.append(_Modifier(int(m[2]), int(m[3]),
                                     None if skill is None else int(skill)))
            except (IndexError, TypeError, ValueError):
                continue
        return tuple(out)


def _view(dogma):
    """The read surface for ``dogma``: None -> the bundled table, an accessor
    object -> itself, a wire dict -> a dict reader. Anything else reads empty
    rather than raising."""
    if dogma is None:
        return _BundledView()
    if hasattr(dogma, "type_attrs") and hasattr(dogma, "effect"):
        return _ModuleView(dogma)
    if isinstance(dogma, dict):
        return _TableView(dogma)
    return _TableView({})


# ── cost ─────────────────────────────────────────────────────────────────────

#: Float slack absorbed before the ceil. ``1 + -80/100`` is 0.19999999999999996
#: in binary floating point, so the Rapier row lands on 49.99999999999999 —
#: BELOW its integer, which ``ceil`` happens to round correctly. The epsilon
#: guards the opposite direction: a factor product that lands a hair ABOVE an
#: exact integer would ceil to N+1 and quietly overstate every cost. (The
#: Venture row, 800 x 0.5 x 0.5, is exact in binary and needs no help.)
_CEIL_EPSILON = 1e-9


def _skill_factor(skill_level, view) -> float:
    """``1 + level x (per-level percent) / 100`` — the TWO-STEP rule. Attribute
    1296 on type 21603 is the bonus PER LEVEL (-10), not the total."""
    try:
        level = int(skill_level)
    except (TypeError, ValueError):
        level = int(DEFAULTS["assumed_skill_level"])
    level = max(0, min(level, SKILL_LEVEL_MAX))
    per_level = view.attrs(CYNO_SKILL_ID).get(ATTR_CONSUMPTION_BONUS)
    if per_level is None:
        per_level = SKILL_BONUS_PER_LEVEL
    return 1.0 + (level * float(per_level)) / 100.0


def _hull_applies_cyno_bonus(hull_type_id, view) -> bool:
    """Does this hull's 1296 actually reduce cyno consumption?

    True only when the hull carries an effect whose modifier row targets
    attribute 714 THROUGH attribute 1296 and is keyed on required skill 21603 —
    which is effect 3526 in the shipped table. The skill-id clause is the whole
    point: a hull that modifies 714 through some other required skill (the
    industrial-cyno Rorqual/Orca family) must not have its bonus read as a
    combat-cyno reduction."""
    for effect_id in view.effects(hull_type_id):
        for mod in view.modifiers(effect_id):
            if (mod.modified_attr == ATTR_CONSUMPTION
                    and mod.modifying_attr == ATTR_CONSUMPTION_BONUS
                    and mod.skill_type_id == CYNO_SKILL_ID):
                return True
    return False


def _hull_factor(hull_type_id, view) -> float:
    """``1 + hull_percent / 100``, or 1.0 when the hull carries no applicable
    bonus (unknown hull, no 1296, or a 1296 keyed on another skill)."""
    if hull_type_id is None:
        return 1.0
    bonus = view.attrs(hull_type_id).get(ATTR_CONSUMPTION_BONUS)
    if bonus is None:
        return 1.0
    if not _hull_applies_cyno_bonus(hull_type_id, view):
        return 1.0
    return 1.0 + float(bonus) / 100.0


def effective_ozone_cost(gen_type_id, hull_type_id, skill_level: int = 5,
                         dogma=None):
    """Liquid Ozone burnt by ONE activation of ``gen_type_id`` on
    ``hull_type_id``, or ``None`` when it cannot be known.

    ``None`` means exactly "no answer" — an unknown generator, a generator the
    table carries no attribute 714 for, or no table at all. Callers must treat
    it as "stay quiet"; there is no plausible default worth guessing.

    ``skill_level`` is the assumed Cynosural Field Theory level (the ESI skills
    scope is not registered — see the spec's decision 1); it is clamped to
    0..5. ``dogma`` injects a table: ``None`` = the bundled one, an object with
    the ``dogma_data`` accessors, or a raw wire-shaped dict.

    Never raises."""
    try:
        view = _view(dogma)
        base = view.attrs(gen_type_id).get(ATTR_CONSUMPTION)
        if base is None:
            return None
        value = float(base) * _skill_factor(skill_level, view) \
            * _hull_factor(hull_type_id, view)
        if value <= 0:
            return 0
        return int(math.ceil(value - _CEIL_EPSILON))
    except Exception:                                       # pragma: no cover
        return None


def is_generator(type_id, dogma=None) -> bool:
    """Is this type a Cynosural Field Generator?

    Group 658 is the answer; the three known ids are the fallback for a type
    the table does not carry (or no table at all), so the fitted-module scan
    keeps working on a box with a missing ``fit_dogma.json.gz``. Never raises."""
    try:
        tid = int(type_id)
    except (TypeError, ValueError):
        return False
    group = _view(dogma).group(tid)
    if group is not None:
        return int(group) == GENERATOR_GROUP
    return tid in KNOWN_GENERATOR_IDS


#: What every generator's name contains, lower-cased. The gamelog parser is
#: deliberately module-agnostic (``is already active`` is emitted for any module
#: re-click), so this is how its output is filtered down to cyno lines.
GENERATOR_NAME_TOKEN = "cynosural field generator"


def is_generator_name(name) -> bool:
    """Does this module name denote a cynosural field generator?

    Case-insensitive substring match on "cynosural field generator", which
    covers all three ("Cynosural Field Generator I", "Covert Cynosural Field
    Generator I", "Industrial Cynosural Field Generator I") and any future
    variant CCP names the same way. The complement of a name check is what the
    O2 gamelog hook needs, because the log line names the module but never its
    type id. Never raises."""
    try:
        return GENERATOR_NAME_TOKEN in str(name or "").lower()
    except Exception:                                       # pragma: no cover
        return False


def generator_cycle_s(gen_type_id, dogma=None) -> float:
    """One activation's cycle time, in SECONDS.

    Attribute 73 is the duration in MILLISECONDS: 600 000 for the standard and
    industrial generators (10 min), 60 000 for the covert one (1 min) —
    measured off the shipped table. An unknown generator, a missing attribute
    or no table at all answer :data:`DEFAULT_CYCLE_S` (600.0) rather than 0,
    because this value is a DEDUPE WINDOW: a zero would collapse the window and
    let a burst of re-click hints each burn an activation, which is the exact
    defect it exists to prevent. Never raises."""
    try:
        ms = _view(dogma).attrs(gen_type_id).get(ATTR_DURATION)
        if ms is None:
            return DEFAULT_CYCLE_S
        seconds = float(ms) / 1000.0
        return seconds if seconds > 0 else DEFAULT_CYCLE_S
    except Exception:                                       # pragma: no cover
        return DEFAULT_CYCLE_S


def generator_short(type_id) -> str:
    """Short display label for a generator id. Unknown ids read "Cyno" rather
    than exposing a raw type id in a toast."""
    try:
        return GENERATOR_SHORT.get(int(type_id), GENERATOR_SHORT_DEFAULT)
    except (TypeError, ValueError):
        return GENERATOR_SHORT_DEFAULT


# ── asset scan ───────────────────────────────────────────────────────────────

#: ESI's ``location_flag`` for a fitted high slot is ``HiSlot0`` .. ``HiSlot7``.
HISLOT_PREFIX = "HiSlot"
CARGO_FLAG = "Cargo"


class ShipCargo(NamedTuple):
    """What the current ship carries that this feature cares about.

    ``generator_type_id`` is the CHOSEN generator when more than one is
    fitted at once (a standard AND a covert cyno on the same Force Recon is a
    real, reported fit): the one with the largest attr-714 BASE consumption
    (before the skill/hull multipliers) — the owner's rule, "when in doubt
    always count the module with the bigger consumption". The hull multiplier
    in :func:`effective_ozone_cost` is IDENTICAL for every generator fitted to
    the same hull, so ranking by base 714 alone always agrees with ranking by
    effective cost; nothing here needs to compute the full cost to choose.
    Ties (equal or both-unknown base) keep the FIRST fitted one seen in asset
    order — a generator the table carries no 714 for sorts as base 0 and is
    still a candidate, so "no generator has a known base" falls out of the
    same tie-break rather than needing its own branch. ``generator_type_ids``
    carries every fitted generator, in asset order, so a caller that needs the
    full fit (not just the one that decides) still has it."""
    generator_type_id: int | None = None
    ozone: int = 0
    generator_type_ids: tuple[int, ...] = ()


#: "Nothing aboard" — the answer for an unknown ship and every degrade path.
EMPTY_CARGO = ShipCargo()


def _int_or_none(value):
    try:
        if isinstance(value, bool):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def scan_ship_assets(assets, ship_item_id, dogma=None) -> ShipCargo:
    """Reduce an ESI ``/assets/`` page to ``(fitted generator, ozone units)``.

    Only rows whose ``location_id`` is ``ship_item_id`` count — the payload is
    the character's WHOLE asset list, so every other hangar, container and ship
    is in there too. A generator counts when its ``location_flag`` starts with
    ``HiSlot`` (fitted, not rattling around the cargo hold); Liquid Ozone counts
    when its flag is exactly ``Cargo`` and its quantities are summed across
    stacks.

    ``ship_item_id`` of ``None``/0 (the character is in a capsule, or the ship
    read failed) answers ``EMPTY_CARGO`` without walking the payload at all.
    Garbage rows — non-dicts, missing keys, unparseable numbers — are skipped
    individually, so one bad row cannot cost the good ones. Never raises.

    **``assets`` MUST be the FULLY paginated asset list.** This function cannot
    tell a short list from an empty hold, so a partial or failed pull reads as
    "generator fitted, 0 ozone" and would fire a confident, wrong toast. A pull
    that did not complete is NO SAMPLE: the caller must skip the observation
    entirely rather than hand over what it managed to fetch (the O2 contract).

    One honest under-count remains: ozone inside a container in the cargo hold
    is a child of the CONTAINER's item_id, not the ship's, so it is invisible
    here. That under-counts and so can only produce a spurious warning, never a
    missed one — the safe direction, and rare enough not to be worth a second
    pass over the payload.

    **When more than one generator is fitted** (a standard AND a covert cyno
    on the same Force Recon is a real fit, not a hypothetical), the returned
    ``generator_type_id`` is the one with the largest attr-714 base
    consumption — see :class:`ShipCargo`. Every fitted generator, in asset
    order, is still returned via ``generator_type_ids``."""
    ship = _int_or_none(ship_item_id)
    if not ship:
        return EMPTY_CARGO
    generator_ids = []
    ozone = 0
    try:
        rows = list(assets or ())
    except TypeError:
        return EMPTY_CARGO
    for row in rows:
        if not isinstance(row, dict):
            continue
        if _int_or_none(row.get("location_id")) != ship:
            continue
        flag = row.get("location_flag")
        if not isinstance(flag, str):
            continue
        type_id = _int_or_none(row.get("type_id"))
        if type_id is None:
            continue
        if flag.startswith(HISLOT_PREFIX):
            if is_generator(type_id, dogma):
                generator_ids.append(type_id)
        elif flag == CARGO_FLAG and type_id == LIQUID_OZONE_TYPE_ID:
            qty = _int_or_none(row.get("quantity"))
            if qty and qty > 0:
                ozone += qty
    generator = _choose_generator(generator_ids, dogma) if generator_ids else None
    return ShipCargo(generator, ozone, tuple(generator_ids))


def _choose_generator(type_ids, dogma):
    """Which fitted generator DECIDES, per :class:`ShipCargo`'s rule: the
    largest attr-714 base consumption, ties keeping the first seen. An id the
    table carries no 714 for reads as base 0 (still a candidate) — never
    raises, and never returns ``None`` for a non-empty ``type_ids``."""
    view = _view(dogma)
    best = type_ids[0]
    try:
        best_base = float(view.attrs(best).get(ATTR_CONSUMPTION) or 0.0)
    except Exception:                                       # pragma: no cover
        best_base = 0.0
    for tid in type_ids[1:]:
        try:
            base = float(view.attrs(tid).get(ATTR_CONSUMPTION) or 0.0)
        except Exception:                                   # pragma: no cover
            base = 0.0
        if base > best_base:
            best, best_base = tid, base
    return best


# ── verdict + copy ───────────────────────────────────────────────────────────

def lights_left(ozone, cost) -> int:
    """How many cyno activations the ozone aboard pays for.

    A cost of ``None`` or <= 0 answers 0 — "we cannot know", which reads the
    same as "none" to every caller and never fabricates a number. Never
    raises."""
    units = _int_or_none(ozone) or 0
    per = _int_or_none(cost)
    if per is None or per <= 0 or units <= 0:
        return 0
    return units // per


class Verdict(NamedTuple):
    fire: bool      # warn now?
    lights: int     # activations the cargo pays for
    text: str       # "N ozone - M activations left" (e.g. "175 ozone - 3 activations left")


def _lights_phrase(ozone: int, lights: int) -> str:
    return f"{ozone} ozone - {lights} activation{'s' if lights != 1 else ''} left"


def verdict(cargo, cost, min_activations) -> Verdict:
    """Should this sample warn, and what does it read as?

    Fires only when a generator is actually FITTED, the cost is known, and the
    lights aboard fall short of ``min_activations``. No generator fitted or an
    unknown cost are both "nothing to say" — the text is still built, because
    the Characters-tab card row shows it whether or not it warns.

    ``text`` is ``"{ozone} ozone - {lights} activation(s) left"`` (e.g.
    "175 ozone - 3 activations left", "50 ozone - 1 activation left",
    "0 ozone - 0 activations left") — it names neither the hull, the
    generator, nor ``min_activations``.

    ``min_activations`` <= 0 disables the warning (a legitimate reading of the
    config, preserved by ``normalize_config``). Never raises."""
    if not isinstance(cargo, ShipCargo):
        cargo = EMPTY_CARGO
    ozone = max(0, _int_or_none(cargo.ozone) or 0)
    lights = lights_left(ozone, cost)
    text = _lights_phrase(ozone, lights)
    need = _int_or_none(min_activations)
    per = _int_or_none(cost)
    fire = bool(
        cargo.generator_type_id is not None
        and per is not None and per > 0
        and need is not None and need > 0
        and lights < need
    )
    return Verdict(fire, lights, text)


#: Toast title prefix, and the fallback title when a character name is
#: unavailable. Short by design — see ``toast_title`` for the full title.
TOAST_TITLE = "Ozone"


def toast_title(char_name) -> str:
    """``"Ozone - {char_name}"`` — e.g. "Ozone - Securitas Protector".

    With several clients open -- tiled, overlapping, or seen only at a
    glance -- a bare "Ozone" does not say WHICH character is low, so the
    title names them. Falls back to plain ``TOAST_TITLE`` when ``char_name``
    is blank, ``None``, or otherwise unusable. Never raises — a non-``str``
    is coerced with ``str()`` first."""
    try:
        name = str(char_name or "").strip()
        if not name:
            return TOAST_TITLE
        return f"{TOAST_TITLE} - {name}"
    except Exception:                                       # pragma: no cover
        return TOAST_TITLE


def toast_body(hull_name, cargo, cost, min_activations) -> str:
    """``"{ozone} ozone - {lights} activation(s) left"`` — e.g.
    "175 ozone - 3 activations left", "50 ozone - 1 activation left",
    "0 ozone - 0 activations left".

    Mentions neither the hull, the generator, "lights" (the internal count,
    yes; the word, no), nor "need N" — the toast title (naming the
    character) and the client it floats over already identify the ship.
    ``hull_name`` and ``min_activations`` are accepted for API stability but
    are unused by the body (``min_activations`` still gates ``verdict()``'s
    ``fire`` decision upstream, just not this string). Never raises."""
    if not isinstance(cargo, ShipCargo):
        cargo = EMPTY_CARGO
    return verdict(cargo, cost, min_activations).text


# ── per-character latch ──────────────────────────────────────────────────────

FIRE = "FIRE"              # a new low-ozone sighting: warn now
HOLD = "HOLD"              # same ship, same ozone, already warned
CLEAR = "CLEAR"            # no generator / enough lights / unknown: latch released
SUPPRESSED = "SUPPRESSED"  # per-character opt-out


class LastSeen(NamedTuple):
    """The most recent sample for a character — what ``observe_cyno_lit``
    decrements. ``cargo``/``cost`` may be ``None`` when nothing is known.

    ``lit_at`` is the ``now`` of the last ACCEPTED cyno-lit event, which is how
    the per-cycle dedupe window is enforced; it is cleared whenever the ship
    changes, because the window is per ``(character, ship_item_id)``."""
    ship_item_id: int | None = None
    cargo: ShipCargo | None = None
    cost: int | None = None
    lit_at: float | None = None


@dataclass
class _CharWatch:
    #: The ``(ship_item_id, ozone)`` the last FIRE latched on, or None.
    latch: tuple | None = None
    #: ``now`` of the most recent observation; None until the first one.
    seen: float | None = None
    last: LastSeen = LastSeen()


def _key(char_key) -> str:
    return str(char_key or "").strip().lower()


@dataclass
class WatchState:
    """Latch bookkeeping for every watched character. Pure + Tk-free.

    **Thread safety.** This object is written from TWO threads: the ESI poller
    (undock/login edges, via ``observe_edge``) and whichever thread the gamelog
    tailer's callback runs on (``observe_cyno_lit``). Every public method —
    readers included — therefore takes ``_lock``, a re-entrant lock (
    ``observe_cyno_lit`` calls ``observe_edge`` beneath it). ``threading`` is
    standard library, so the module stays pure. Nothing outside this class may
    reach into ``_chars``.

    ``observe_edge`` is called on an UNDOCK or LOGIN edge, not on every poll,
    and returns ``FIRE`` only when the sample is new evidence of a low hold.
    The latch is keyed on ``(ship_item_id, ozone)``, which is what makes the
    re-arming honest:

    * the same ship with the same ozone is the same warning — ``HOLD``. That is
      not a theoretical case: an undock and a login edge routinely arrive as a
      burst for the same character (logging in undocked fires both), and the
      wiring may re-observe an edge it is unsure about. ``HOLD`` is what
      collapses that burst into one toast.
    * a different ship, or the same ship after the number moved (a restock, or
      our own cyno-lit decrement), is a new fact — ``FIRE``;
    * a generator that is gone, or enough lights, releases the latch outright —
      ``CLEAR``, so the next dip fires again.

    ``blind_gap_s`` is the third release, and it exists for the same reason as
    ``implant_reminder.ReminderState.blind_gap_s``: **the signal is sampled,
    not streamed.** Between two samples a pilot can undock, burn a cyno, dock,
    restock to the same round number and undock again; a latch cannot vouch for
    a window nobody watched. After a gap that long the latch is dropped and the
    next low sample warns. At worst that is ONE extra toast after a minute of
    blindness, against a silently eaten warning — the error direction the owner
    asked for.

    **The cyno-lit decrement is deduped per generator CYCLE.** Neither gamelog
    line this feature reads is an activation: both are REJECTED-action lines (a
    re-click hint, and a dock refusal while the field burns) and a pilot can
    produce a dozen of either during one 10-minute cycle — and the tailer
    replays a whole file on rotation. Counting each one would walk a Rapier's
    500 ozone down to a fabricated "3 lights" in seconds. So an accepted event
    stamps ``LastSeen.lit_at`` and every further event inside
    ``generator_cycle_s`` of it answers ``HOLD`` without touching the figure.
    The one deliberate exception: once the hold reads ZERO, an accepted event
    re-``FIRE``s rather than holding — "you are dry" is worth repeating."""

    #: Warn below this many lights. Comes from ``normalize_config``.
    min_activations: int = 4

    #: How long the engine may go unfed before a latch stops being trustworthy.
    blind_gap_s: float = 60.0

    _chars: dict = field(default_factory=dict)
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    # -- bookkeeping --------------------------------------------------------
    def forget(self, char_key) -> None:
        """Drop all state for a character (client closed / logged out)."""
        with self._lock:
            self._chars.pop(_key(char_key), None)

    def last(self, char_key):
        """The most recent sample for a character, or ``None`` if unknown.

        ``LastSeen`` is an immutable tuple, so the value handed back cannot be
        used to mutate this object's state from another thread."""
        with self._lock:
            st = self._chars.get(_key(char_key))
            return None if st is None else st.last

    def remember(self, char_key, ship_item_id, cargo, cost) -> None:
        """Record a sample WITHOUT evaluating it — the seam the wiring uses to
        seed a character from a card refresh so a later cyno-lit line has a
        figure to decrement. Touches neither the latch nor the blind-gap
        clock."""
        k = _key(char_key)
        if not k:
            return
        with self._lock:
            st = self._chars.setdefault(k, _CharWatch())
            st.last = self._sample(st.last, ship_item_id, cargo, cost)

    # -- the machine --------------------------------------------------------
    def observe_edge(self, char_key, ship_item_id, cargo, cost, now, *,
                     disabled: bool = False) -> str:
        """Advance one character by one undock/login edge; returns one verb.

        ``now`` is a monotonic timestamp. ``disabled`` is the per-character
        opt-out; like the implant reminder it still LATCHES, so turning a
        character back on mid-session does not retroactively fire for a ship it
        was already sitting in. Never raises."""
        k = _key(char_key)
        if not k:
            return CLEAR
        try:
            ts = float(now)
        except (TypeError, ValueError):
            ts = 0.0

        with self._lock:
            st = self._chars.setdefault(k, _CharWatch())
            previous = st.seen
            st.seen = ts
            st.last = self._sample(st.last, ship_item_id, cargo, cost)
            ident = (st.last.ship_item_id,
                     st.last.cargo.ozone if st.last.cargo is not None else None)

            if disabled:
                st.latch = ident
                return SUPPRESSED

            if not verdict(st.last.cargo, cost, self.min_activations).fire:
                st.latch = None
                return CLEAR

            blind = previous is None or (ts - previous) > self._blind_gap()
            if st.latch is not None and st.latch == ident and not blind:
                return HOLD
            st.latch = ident
            return FIRE

    def observe_cyno_lit(self, char_key, now, *, disabled: bool = False,
                         dogma=None) -> str:
        """A cyno-is-burning gamelog line: burn ONE activation locally and
        re-evaluate.

        ESI will not show the burn for up to an hour (the assets endpoint is
        cached), so the last known figure is decremented by one ``cost``. With
        no figure known — an unwatched character, no generator, an unknown cost
        — the answer is ``CLEAR`` and nothing is invented.

        At most one decrement per generator cycle per ``(character, ship)``:
        the lines that feed this are repeatable rejections, not activations, so
        an event inside ``generator_cycle_s`` of the last accepted one answers
        ``HOLD`` and changes nothing. An accepted event on an already-EMPTY
        hold releases the latch so it re-``FIRE``s. ``dogma`` injects the table
        the cycle time is read from. Never raises."""
        with self._lock:
            st = self._chars.get(_key(char_key))
            if st is None:
                return CLEAR
            cargo, cost = st.last.cargo, st.last.cost
            if not isinstance(cargo, ShipCargo) or cargo.generator_type_id is None:
                return CLEAR
            per = _int_or_none(cost)
            if per is None or per <= 0:
                return CLEAR

            try:
                ts = float(now)
            except (TypeError, ValueError):
                ts = 0.0
            window = generator_cycle_s(cargo.generator_type_id, dogma)
            if st.last.lit_at is not None and (ts - st.last.lit_at) < window:
                return HOLD

            burnt = ShipCargo(cargo.generator_type_id, max(0, cargo.ozone - per),
                             cargo.generator_type_ids)
            if burnt.ozone <= 0:
                # Dry. Release the latch so the re-evaluation below FIREs even
                # though the figure did not move — being out of ozone with a
                # cyno fitted is worth saying again, once per cycle.
                st.latch = None
            verb = self.observe_edge(char_key, st.last.ship_item_id, burnt, per,
                                     ts, disabled=disabled)
            # Stamped AFTER observe_edge, which rebuilds ``last``.
            st.last = st.last._replace(lit_at=ts)
            return verb

    # -- helpers ------------------------------------------------------------
    def _blind_gap(self) -> float:
        """The blind-gap window as a float. A None or hostile value on the
        dataclass field falls back to the documented 60 s rather than raising
        inside the state machine."""
        try:
            gap = float(self.blind_gap_s)
        except (TypeError, ValueError):
            return 60.0
        return gap if gap > 0 else 60.0

    @staticmethod
    def _sample(previous, ship_item_id, cargo, cost) -> LastSeen:
        """Build the new ``LastSeen``, carrying ``lit_at`` forward only while
        the SHIP is unchanged — the dedupe window is per (character, ship), so
        stepping into a different hull starts a fresh one."""
        if not isinstance(cargo, ShipCargo):
            cargo = None
        ship = _int_or_none(ship_item_id)
        lit_at = previous.lit_at if (previous is not None
                                     and previous.ship_item_id == ship) else None
        return LastSeen(ship, cargo, _int_or_none(cost), lit_at)
