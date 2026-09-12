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
    (28646 Covert,   22436 Sin,    V) -> 25     (Black Ops: NO bonus)
    (52694 Indy,     32880 Venture, V) -> 200   800 x 0.5 x 0.5
    (21096 Cyno I,   11963 Rapier, 0) -> 100    500 x 1.0 x 0.2

Liquid Ozone is type **16273**. The bundled table does not model it (it is a
charge nothing in the fit sim modifies) and ``fit_types.json`` carries no name
for it either, so the id is hardcoded here and the display string is ours.
"""

from __future__ import annotations

import math
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
EFFECT_CYNO_CONSUMPTION = 3526    # LocationRequiredSkillModifier(714 <- 1296, skill 21603)

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
# It is a deliberate SUPERSET of ``ship_classes.CYNO_LOSS_GROUPS`` (which covers
# only the combat-cyno victim hulls CynoCheck cares about): a false positive
# costs one cached assets read, a false negative is a silently missed warning —
# the whole failure mode this feature exists to close.
#
# Group ids and names are ``type_catalog.SHIP_GROUP_NAMES``; the two Venture
# entries were MEASURED off the bundled table (see the note below).
CYNO_HULL_GROUP_NAMES = {
    # -- combat / covert cyno hulls (superset of CYNO_LOSS_GROUPS) --
    830: "Covert Ops",                  # covert cyno
    833: "Force Recon Ship",            # normal OR covert cyno; carries the -80 bonus
    834: "Stealth Bomber",              # covert cyno
    894: "Heavy Interdiction Cruiser",  # normal cyno
    898: "Black Ops",                   # covert cyno; NO SDE bonus (see the docstring)
    906: "Combat Recon Ship",           # sibling of Force Recon; not in CYNO_LOSS_GROUPS
    963: "Strategic Cruiser",           # covert cyno via the covert subsystem
    # -- industrial cyno hulls --
    # 25 "Frigate" is where the SDE puts the VENTURE (32880) and its Vespera
    # variant (89648) — measured off fit_dogma.json.gz, not assumed. There is
    # no narrower group containing them, so the whole T1 frigate group is in.
    # The cost is bounded: the wiring reads assets only on an edge and caches
    # per ship_item_id for asset_ttl_s.
    25: "Frigate (Venture)",
    28: "Hauler",
    380: "Deep Space Transport",
    463: "Mining Barge",
    543: "Exhumer",
    883: "Capital Industrial Ship",
    941: "Industrial Command Ship",
    1202: "Blockade Runner",
    1283: "Expedition Frigate",         # Prospect / Endurance
}

#: The gate itself. Frozen so no consumer can widen it in place.
CYNO_HULL_GROUPS = frozenset(CYNO_HULL_GROUP_NAMES)


def is_cyno_hull_group(group_id) -> bool:
    """Whether a hull group is worth an assets read. Total: a non-int answers
    False rather than raising."""
    try:
        return int(group_id) in CYNO_HULL_GROUPS
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

#: Float slack absorbed before the ceil. ``500 x 0.5 x (1 + -80/100)`` lands on
#: 49.999999999999993 in binary floating point and ``800 x ...`` can land a
#: hair ABOVE its integer; without the epsilon one of those two directions
#: turns a clean 50 into a 51.
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
    """What the current ship carries that this feature cares about."""
    generator_type_id: int | None = None
    ozone: int = 0


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
    individually, so one bad row cannot cost the good ones. Never raises."""
    ship = _int_or_none(ship_item_id)
    if not ship:
        return EMPTY_CARGO
    generator = None
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
            if generator is None and is_generator(type_id, dogma):
                generator = type_id
        elif flag == CARGO_FLAG and type_id == LIQUID_OZONE_TYPE_ID:
            qty = _int_or_none(row.get("quantity"))
            if qty and qty > 0:
                ozone += qty
    return ShipCargo(generator, ozone)


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
    text: str       # "N ozone = M lights" / "N ozone = no lights"


def _lights_phrase(ozone: int, lights: int) -> str:
    if lights <= 0:
        tail = "no lights"
    elif lights == 1:
        tail = "1 light"
    else:
        tail = f"{lights} lights"
    return f"{ozone} ozone = {tail}"


def verdict(cargo, cost, min_activations) -> Verdict:
    """Should this sample warn, and what does it read as?

    Fires only when a generator is actually FITTED, the cost is known, and the
    lights aboard fall short of ``min_activations``. No generator fitted or an
    unknown cost are both "nothing to say" — the text is still built, because
    the Characters-tab card row shows it whether or not it warns.

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


#: Toast title. Short by design — the toast is placed over a live client.
TOAST_TITLE = "Ozone"


def toast_body(hull_name, cargo, cost, min_activations) -> str:
    """``"{Hull} · {Generator short} · {ozone} ozone = {lights} lights (need N)"``.

    Zero lights read "no lights" (so "0 ozone = no lights" for an empty hold),
    and one reads "1 light". Never raises."""
    hull = str(hull_name or "").strip() or "Ship"
    if not isinstance(cargo, ShipCargo):
        cargo = EMPTY_CARGO
    short = generator_short(cargo.generator_type_id)
    need = max(0, _int_or_none(min_activations) or 0)
    return f"{hull} · {short} · {verdict(cargo, cost, need).text} (need {need})"


# ── per-character latch ──────────────────────────────────────────────────────

FIRE = "FIRE"              # a new low-ozone sighting: warn now
HOLD = "HOLD"              # same ship, same ozone, already warned
CLEAR = "CLEAR"            # no generator / enough lights / unknown: latch released
SUPPRESSED = "SUPPRESSED"  # per-character opt-out


class LastSeen(NamedTuple):
    """The most recent sample for a character — what ``observe_cyno_lit``
    decrements. ``cargo``/``cost`` may be ``None`` when nothing is known."""
    ship_item_id: int | None = None
    cargo: ShipCargo | None = None
    cost: int | None = None


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

    ``observe_edge`` is called on an UNDOCK or LOGIN edge (not every poll) and
    returns ``FIRE`` only when the sample is new evidence of a low hold. The
    latch is keyed on ``(ship_item_id, ozone)``, which is what makes the
    re-arming honest:

    * the same ship with the same ozone is the same warning — ``HOLD``;
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
    asked for."""

    #: Warn below this many lights. Comes from ``normalize_config``.
    min_activations: int = 4

    #: How long the engine may go unfed before a latch stops being trustworthy.
    blind_gap_s: float = 60.0

    _chars: dict = field(default_factory=dict)

    # -- bookkeeping --------------------------------------------------------
    def forget(self, char_key) -> None:
        """Drop all state for a character (client closed / logged out)."""
        self._chars.pop(_key(char_key), None)

    def last(self, char_key):
        """The most recent sample for a character, or ``None`` if unknown."""
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
        st = self._chars.setdefault(k, _CharWatch())
        st.last = self._sample(ship_item_id, cargo, cost)

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

        st = self._chars.setdefault(k, _CharWatch())
        previous = st.seen
        st.seen = ts
        st.last = self._sample(ship_item_id, cargo, cost)
        ident = (st.last.ship_item_id, st.last.cargo.ozone
                 if st.last.cargo is not None else None)

        if disabled:
            st.latch = ident
            return SUPPRESSED

        if not verdict(st.last.cargo, cost, self.min_activations).fire:
            st.latch = None
            return CLEAR

        blind = previous is None or (ts - previous) > float(self.blind_gap_s)
        if st.latch is not None and st.latch == ident and not blind:
            return HOLD
        st.latch = ident
        return FIRE

    def observe_cyno_lit(self, char_key, now, *, disabled: bool = False) -> str:
        """A cyno-active gamelog line: burn one activation locally and
        re-evaluate.

        ESI will not show the burn for up to an hour (the assets endpoint is
        cached), so the last known figure is decremented by one ``cost``. With
        no figure known — an unwatched character, no generator, an unknown cost
        — the answer is ``CLEAR`` and nothing is invented. Never raises."""
        st = self._chars.get(_key(char_key))
        if st is None:
            return CLEAR
        cargo, cost = st.last.cargo, st.last.cost
        if not isinstance(cargo, ShipCargo) or cargo.generator_type_id is None:
            return CLEAR
        per = _int_or_none(cost)
        if per is None or per <= 0:
            return CLEAR
        burnt = ShipCargo(cargo.generator_type_id, max(0, cargo.ozone - per))
        return self.observe_edge(char_key, st.last.ship_item_id, burnt, per,
                                 now, disabled=disabled)

    # -- helpers ------------------------------------------------------------
    @staticmethod
    def _sample(ship_item_id, cargo, cost) -> LastSeen:
        if not isinstance(cargo, ShipCargo):
            cargo = None
        return LastSeen(_int_or_none(ship_item_id), cargo, _int_or_none(cost))
