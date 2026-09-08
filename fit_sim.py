"""Generic dogma modifier engine: evaluate a parsed fit's attributes.

Pure stdlib, no Tk, no network, no ``fc_gui``.  Layering (imports flow only
downward)::

    dogma_data <- fit_sim <- fit_sim_links <- fit_sim_stats <- {panel, fleet}

Nothing here knows what a "weapon" or a "resist" is: it turns a
:class:`fit_models.ParsedFit` plus the bundled dogma table into a tree of
:class:`Item` objects whose ``attrs`` carry the fully modified values.  Deriving
DPS / EHP / range from those numbers is the next layer's job.

The engine implements ten numbered rules; every rule number appears in the
docstring of the code that implements it, so a reviewer can walk them:

1. **States** -- ``_ALLOWED_CATEGORIES`` / :func:`_effect_defs`.
2. **Charges** -- :func:`build_fit`, :func:`_other_item`.
3. **Domains** -- :func:`_domain`.
4. **Funcs** -- :func:`_targets`.
5. **Skill levels** -- :func:`_build_skill_prototypes`, :func:`_plan`.
6. **Operation order** -- :func:`_apply`.
7. **Stacking penalty** -- :func:`stacking_multiplier`, :func:`_penalised`,
   :func:`_combined_multiplier`.
8. **Dependency passes** -- :func:`evaluate`.
9. **Fleet buffs** -- :func:`_buff_plan`.
10. **Never raise for data problems** -- :func:`_make_item`, :func:`_record`,
    :func:`_skippable_modifier`.

**Never loads anything.**  ``dogma_data.load()`` is the caller's business, on a
worker thread; every entry point here raises
:class:`dogma_data.DogmaUnavailable` if no table is installed.  That is the ONE
exception to rule 10: a missing table is a caller error, not fit data.

Evaluation is planned ONCE and then replayed
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Everything about a contribution except its magnitude is fixed for the whole
evaluation: which modifier rows are parseable, which items they reach, whether
they stack-penalise, what an attribute's default is.  Only the VALUE a source
carries changes from pass to pass.  :func:`_plan` therefore resolves all of that
once into :class:`_Entry` (one source x modifier that reaches something) and
:class:`_Group` (every contribution to one item x attribute, already ordered by
:data:`OP_ORDER`), and each pass then just re-reads the entry values and folds
the groups.  A modifier that reaches nothing -- most of a fit's ~580 skills --
costs one resolution instead of one per pass.

Bounded module state
~~~~~~~~~~~~~~~~~~~~

The engine owns exactly ONE module-level cache, the All-V skill prototype set
(``_SKILL_PROTOTYPES`` and the four values built with it -- ``_SKILL_LEVELS``,
``_SKILL_GAPS``, ``_SKILL_NOTES``, plus the ``_SKILL_PROTOTYPES_BUILD`` /
``_SKILL_PROTOTYPES_TABLE`` identity it was built for).  It is BOUNDED: one
immutable tuple of at most one Item per skill the table keeps (588 today), never
appended to, rebuilt wholesale when a different table is installed and dropped
by :func:`_reset_prototypes_for_tests`.  Rebuilding those ~580 items per fit was
8 of the 9 ms :func:`build_fit` used to cost.  Per-fit skill items SHARE the
prototype's ``base`` dict, ``effects``/``resolved`` tuples and cached skill
requirements -- all immutable by contract -- and copy only the live ``attrs``.

v1 simplifications, all deliberate and recorded in ``FitState.unmodeled`` where
they touch a specific fit:

* every fitted module is ``active`` unless the parser marked it ``offline`` --
  EFT/DNA carry no "online but not active" notion, so the engine has no way to
  produce ``STATE_ONLINE`` from a parsed fit (a caller may still set it on an
  :class:`Item` by hand, and the state machine honours it);
* overload (effect category 5) is never applied;
* ``targetID`` / ``structureID`` modifiers are skipped (no target is modelled);
* ``EffectStopper`` rows, and rows whose ``operation`` is not one of the nine
  dogma op codes, are skipped SILENTLY -- they are a row shape the engine has
  no arithmetic for, not a gap in what the fit models, so they are never
  reported (see :func:`_skippable_modifier`);
* T3 subsystems and cargo (which is where the parser puts implants) are not
  evaluated -- subsystems are recorded as unmodeled, cargo is ignored.  Implants
  reach the engine through ``build_fit(parsed, implants=[...])`` instead, from a
  caller that knows which ones the pilot is wearing;
* implants (category 20) do not stack-penalise: rule 7 is implemented as
  "module or drone source", which is what the plan specifies;
* mutaplasmid rolls never reach here (the parser strips them).

Skill-sourced modelling gaps are NOT user-facing.  ~340 of the table's skills
carry an effect with no modifierInfo (learning effects, skill markers), which
would drown a fit's own handful of real gaps.  They land in
``FitState.unmodeled_skill_effects`` as ``(skill_type_id, effect_id)`` pairs;
``FitState.unmodeled`` keeps fit-item gaps only.
"""

from __future__ import annotations

import math
import threading
from dataclasses import dataclass, field
from typing import Sequence, NamedTuple

import dogma_data
from fit_models import ParsedFit

# ── dogma operation codes (rule 6) ───────────────────────────────────────────
OP_PRE_ASSIGN = -1
OP_PRE_MUL = 0
OP_PRE_DIV = 1
OP_MOD_ADD = 2
OP_MOD_SUB = 3
OP_POST_MUL = 4
OP_POST_DIV = 5
OP_POST_PERCENT = 6
OP_POST_ASSIGN = 7

#: Application order. Assign ops bracket the arithmetic ones.
OP_ORDER = (OP_PRE_ASSIGN, OP_PRE_MUL, OP_PRE_DIV, OP_MOD_ADD, OP_MOD_SUB,
            OP_POST_MUL, OP_POST_DIV, OP_POST_PERCENT, OP_POST_ASSIGN)
_ASSIGN_OPS = frozenset({OP_PRE_ASSIGN, OP_POST_ASSIGN})
#: ``operation -> its place in OP_ORDER``, for sorting a group's few ops without
#: walking all nine.
_OP_RANK = {operation: index for index, operation in enumerate(OP_ORDER)}
#: Rule 10: every operation code the engine has arithmetic for. The SDE carries
#: exactly one row outside it (``operation`` 9, on ``skillEffect``) plus the ten
#: ``EffectStopper`` rows whose ``operation`` is absent altogether.
VALID_OPERATIONS = frozenset(OP_ORDER)
#: The ops a stacking penalty can apply to (rule 7).
MULTIPLICATIVE_OPS = frozenset({OP_PRE_MUL, OP_PRE_DIV, OP_POST_MUL,
                                OP_POST_DIV, OP_POST_PERCENT})

# ── stacking (rule 7) ────────────────────────────────────────────────────────
STACKING_CONST = 2.67

# ── attributes ───────────────────────────────────────────────────────────────
ATTR_SKILL_LEVEL = 280
#: requiredSkill1..6 -- the attributes that name a type's prerequisite skills.
REQUIRED_SKILL_ATTRS = (182, 183, 184, 1285, 1289, 1290)
#: requiredSkill1Level..6 -- carried for completeness; the engine trains All V.
REQUIRED_SKILL_LEVEL_ATTRS = (277, 278, 279, 1286, 1287, 1288)

# ── item states (rule 1) ─────────────────────────────────────────────────────
STATE_OFFLINE = "offline"
STATE_ONLINE = "online"
STATE_ACTIVE = "active"

# ── dogma effect categories ──────────────────────────────────────────────────
EFFECT_PASSIVE = 0
EFFECT_ACTIVE = 1
EFFECT_TARGET = 2
EFFECT_ONLINE = 4
EFFECT_OVERLOAD = 5

#: Rule 1. Offline contributes nothing; online adds passive + online; active
#: adds the active and target categories on top. Overload is never in any set.
_ALLOWED_CATEGORIES = {
    STATE_OFFLINE: frozenset(),
    STATE_ONLINE: frozenset({EFFECT_PASSIVE, EFFECT_ONLINE}),
    STATE_ACTIVE: frozenset({EFFECT_PASSIVE, EFFECT_ACTIVE, EFFECT_TARGET,
                             EFFECT_ONLINE}),
}

# ── inventory categories ─────────────────────────────────────────────────────
CATEGORY_CHARACTER = 1
CATEGORY_SHIP = 6
CATEGORY_MODULE = 7
CATEGORY_CHARGE = 8
CATEGORY_SKILL = 16
CATEGORY_DRONE = 18
CATEGORY_IMPLANT = 20

#: Rule 7: only module- and drone-sourced multipliers stack-penalise. Ships,
#: skills, implants and charges are exempt.
PENALISED_SOURCE_CATEGORIES = frozenset({CATEGORY_MODULE, CATEGORY_DRONE})

# ── modifier domains (rule 3) ────────────────────────────────────────────────
DOMAIN_SHIP = "shipID"
DOMAIN_CHAR = "charID"
#: "the item that carries the effect" -- self. 221 SDE modifiers use it, and it
#: is the first half of the two-step skill-bonus pattern (rule 5).
DOMAIN_ITEM = "itemID"
DOMAIN_OTHER = "otherID"
DOMAIN_TARGET = "targetID"
DOMAIN_STRUCTURE = "structureID"

# ── modifier funcs (rule 4) ──────────────────────────────────────────────────
FUNC_ITEM = "ItemModifier"
FUNC_LOCATION = "LocationModifier"
FUNC_LOCATION_GROUP = "LocationGroupModifier"
FUNC_LOCATION_SKILL = "LocationRequiredSkillModifier"
FUNC_OWNER_SKILL = "OwnerRequiredSkillModifier"
#: Rule 10: not a modifier at all -- it names an effect to suppress, and its
#: rows carry no modified/modifying attribute or operation.
FUNC_EFFECT_STOPPER = "EffectStopper"

#: Rule 4: the classic dogma self-reference convention -- a required-skill
#: modifier whose ``skillTypeID`` is -1 means "the skill this effect is on",
#: i.e. the SOURCE item's own type id. The shipped SDE carries no such row; the
#: generator uses it for curated skill-effect overrides, and an engine that read
#: it as a literal type id would silently reach nothing.
SELF_SKILL = -1

# ── dbuff modifier kinds (rule 9) ────────────────────────────────────────────
BUFF_ITEM = "item"
BUFF_LOCATION = "location"
BUFF_LOCATION_GROUP = "location_group"
BUFF_LOCATION_SKILL = "location_skill"

#: Rule 8: fixed-point evaluation is capped so a cyclic table cannot hang the
#: worker. Four is what pyfa's lazy recursion reaches in practice.
MAX_PASSES = 4

#: Every skill is trained to V (the pyfa default and the FC convention).
ALL_V = 5

#: Cache miss marker -- ``None`` is a legitimate cached value (an effect the
#: table does not carry), so it cannot double as "not looked up yet".
_UNCACHED = object()

#: "this attribute was absent", which is NOT the same as "it was zero" when the
#: convergence check asks whether a pass changed anything.
_MISSING = object()


class Buff(NamedTuple):
    """One fleet/warfare buff to apply to a fit (rule 9)."""
    buff_id: int
    value: float


class Item:
    """One evaluated thing: the ship, a module, a charge, a drone stack, a
    skill, an implant, or the character.

    ``base`` is the type's own attribute values and is NEVER mutated after
    construction; ``attrs`` is the live value store the engine rewrites on every
    pass.  An attribute absent from both reads as its dogma default (see
    :func:`attr`).

    ``resolved`` is an optional pre-filtered ``((EffectDef, modifiers), ...)``
    tuple for the ACTIVE state, carried by the shared skill prototypes so a fit
    never re-decodes ~600 skill effects; ``None`` means "resolve me normally".
    """

    __slots__ = ("type_id", "group_id", "category_id", "attrs", "base", "state",
                 "charge", "parent", "quantity", "effects", "unmodeled_effects",
                 "resolved", "_skill_reqs")

    def __init__(self, type_id, group_id, category_id, attrs, effects=(), *,
                 state=STATE_ACTIVE, quantity=1, unmodeled_effects=(),
                 charge=None, parent=None):
        self.type_id = int(type_id)
        self.group_id = int(group_id)
        self.category_id = int(category_id)
        self.base = dict(attrs)
        self.attrs = dict(attrs)
        self.effects = tuple(effects)
        self.state = state
        self.quantity = int(quantity)
        self.unmodeled_effects = tuple(unmodeled_effects)
        self.charge = charge
        self.parent = parent
        self.resolved = None
        self._skill_reqs = None

    def skill_requirements(self) -> tuple[int, ...]:
        """The type ids of this item's prerequisite skills (attributes 182 /
        183 / 184 / 1285 / 1289 / 1290).  Read off ``base``: a requirement is
        never modified, and reading the live store would make the answer depend
        on which pass asked."""
        if self._skill_reqs is None:
            reqs = []
            for attr_id in REQUIRED_SKILL_ATTRS:
                value = self.base.get(attr_id)
                if value:
                    reqs.append(int(value))
            self._skill_reqs = tuple(reqs)
        return self._skill_reqs

    def __repr__(self):                                  # pragma: no cover
        return (f"Item(type_id={self.type_id}, state={self.state!r}, "
                f"quantity={self.quantity})")


@dataclass
class FitState:
    """A fit as items, plus everything the engine learned building it."""

    ship: Item
    modules: list[Item]
    drones: list[Item]
    skills: dict[int, int]                    # skill type id -> level (All V)
    unmodeled: list[str]                      # human strings, deduped
    character: Item | None = None
    skill_items: list[Item] = field(default_factory=list)
    implants: list[Item] = field(default_factory=list)
    passes: int = 0                           # rule 8, recorded for the tests
    #: Rule 8: True once a pass changed nothing, False while the fit is
    #: unevaluated and False when :data:`MAX_PASSES` cut the loop short (the
    #: numbers are then the best 4 passes could do, not a fixed point).
    converged: bool = False
    unmodeled_keys: set = field(default_factory=set)
    #: ``(skill_type_id, effect_id)`` for every skill effect the table cannot
    #: model. Deliberately NOT in ``unmodeled``: ~340 of them exist on every
    #: fit, they say nothing about the fit, and they would bury its own gaps.
    unmodeled_skill_effects: set = field(default_factory=set)

    @property
    def charges(self) -> list[Item]:
        return [m.charge for m in self.modules if m.charge is not None]

    def all_items(self) -> list[Item]:
        """Every item the engine evaluates, ship location first."""
        items = [self.ship]
        for mod_item in self.modules:
            items.append(mod_item)
            if mod_item.charge is not None:
                items.append(mod_item.charge)
        items.extend(self.drones)
        items.extend(self.implants)
        items.extend(self.skill_items)
        if self.character is not None:
            items.append(self.character)
        return items

    def ship_location(self) -> list[Item]:
        """Rule 4: the ship location holds the ship, its modules, their charges
        and the drones."""
        items = [self.ship]
        for mod_item in self.modules:
            items.append(mod_item)
            if mod_item.charge is not None:
                items.append(mod_item.charge)
        items.extend(self.drones)
        return items

    def char_location(self) -> list[Item]:
        """Rule 4: the character location holds the skills and the implants.
        The ship and its contents are NOT character-located."""
        return list(self.skill_items) + list(self.implants)

    def owned_items(self) -> list[Item]:
        """Rule 4: the character-OWNED items an ``OwnerRequiredSkillModifier``
        reaches -- drones and charges (both belong to the character, not the
        hull)."""
        return list(self.drones) + self.charges


# ===========================================================================
# rule 7 -- the stacking ladder
# ===========================================================================

def stacking_multiplier(index: int) -> float:
    """The penalty factor for the ``index``-th (0-based) penalised modifier:
    ``exp(-(index / 2.67)**2)`` -- the canonical 100 / 86.9 / 57.1 / 28.3 /
    10.5 / 3.0 % ladder."""
    return math.exp(-((index / STACKING_CONST) ** 2))


# ===========================================================================
# building
# ===========================================================================

def _record(state_unmodeled: list, seen: set, key: tuple, text: str) -> None:
    """Rule 10: note a data problem once, never raise. ``key`` dedupes so a fit
    with eight identical launchers reports one line, not eight."""
    if key in seen:
        return
    seen.add(key)
    state_unmodeled.append(text)


def _make_item(type_id, *, state, quantity, unmodeled, seen, category=None,
               skill_gaps=None):
    """Build one :class:`Item` from the table, or None when the type is unusable.

    Rule 10 -- TOTAL over the table's shape.  An unknown type is recorded and
    skipped; so is a type whose row the accessors cannot read at all (a missing
    ``g``/``c``, an odd-length ``a`` list -- ``dogma_data`` raises rather than
    guessing at a corrupt row), which is recorded as ``corrupt type N``.
    Neither ever propagates an exception to the caller.

    Effects the table cannot model (absent, or present with no modifiers) are
    recorded once per type and remembered on ``Item.unmodeled_effects`` -- EXCEPT
    on a skill, whose gaps go to ``skill_gaps`` instead (see the module
    docstring).
    """
    if not dogma_data.has_type(type_id):
        _record(unmodeled, seen, ("type", type_id), f"unknown type {type_id}")
        return None
    try:
        attrs = dogma_data.type_attrs(type_id)
        effect_ids = dogma_data.type_effects(type_id)
        group_id = dogma_data.type_group(type_id)
        category_id = (dogma_data.type_category(type_id) if category is None
                       else int(category))
    except (KeyError, ValueError, TypeError):
        _record(unmodeled, seen, ("corrupt", type_id), f"corrupt type {type_id}")
        return None

    is_skill = category_id == CATEGORY_SKILL
    unmodeled_effects = []
    for effect_id in effect_ids:
        definition = dogma_data.effect(effect_id)
        if definition is not None and definition.modifiers:
            continue
        if is_skill:
            if skill_gaps is not None:
                skill_gaps.add((int(type_id), int(effect_id)))
            continue
        unmodeled_effects.append(effect_id)
        _record(unmodeled, seen, ("effect", type_id, effect_id),
                f"effect {effect_id} on type {type_id}")
    return Item(type_id, group_id, category_id, attrs, effect_ids, state=state,
                quantity=quantity, unmodeled_effects=unmodeled_effects)


# ---------------------------------------------------------------------------
# the ONE module-level cache: the All-V skill prototype set
# ---------------------------------------------------------------------------
#: The prototypes themselves -- at most one immutable Item per kept skill.
_SKILL_PROTOTYPES: tuple[Item, ...] | None = None
#: The ``dogma_data.sde_build()`` the prototypes were built for.
_SKILL_PROTOTYPES_BUILD: int | None = None
#: A strong reference to the table OBJECT they were built from. The build number
#: alone cannot tell two tables apart (every hand-built test table declares the
#: same one), and dogma_data exposes no public handle, so the module attribute
#: is read here for identity ONLY -- never indexed, never written. Holding the
#: reference is what makes the ``is`` test sound: a dropped table's id could
#: otherwise be recycled by the next one.
_SKILL_PROTOTYPES_TABLE = None
#: ``{skill_type_id: 5}`` for EVERY kept skill, effect-less ones included.
_SKILL_LEVELS = None
#: ``{(skill_type_id, effect_id)}`` the table cannot model (see the docstring).
_SKILL_GAPS = frozenset()
#: ``unmodeled`` lines for skill types the table itself is broken about --
#: unknown or corrupt rows, which ARE worth showing (they are table defects,
#: not the ~340 routine effect gaps).
_SKILL_NOTES = ()
_PROTO_LOCK = threading.Lock()


def _clone_skill_item(prototype: Item) -> Item:
    """A per-fit skill item sharing everything immutable with ``prototype``.

    ``base``, ``effects``, ``resolved`` and the cached skill requirements are
    read-only by contract, so every fit can point at the prototype's; only
    ``attrs`` is copied, because evaluation rewrites it (a skill's own
    ``itemID`` ``preMul`` scales its per-level bonus attribute in place).
    """
    item = Item.__new__(Item)
    item.type_id = prototype.type_id
    item.group_id = prototype.group_id
    item.category_id = prototype.category_id
    item.base = prototype.base                  # SHARED -- never mutate
    item.attrs = dict(prototype.base)
    item.effects = prototype.effects
    item.state = STATE_ACTIVE
    item.quantity = 1
    item.unmodeled_effects = ()
    item.charge = None
    item.parent = None
    item.resolved = prototype.resolved
    item._skill_reqs = prototype._skill_reqs
    return item


def _build_skill_prototypes():
    """Build the All-V skill set for the installed table (rule 5).

    Every kept skill gets its level in the returned levels map; only skills that
    CARRY an effect get an item.  An effect-less skill can never be a modifier
    source (a source has to carry the effect) and nothing reads an inert item's
    values, so an item for it would be pure cost -- ~590 per fit, of which a
    handful matter.
    """
    notes: list[str] = []
    seen: set = set()
    gaps: set = set()
    prototypes: list[Item] = []
    levels: dict[int, int] = {}
    for raw_id in dogma_data.skill_type_ids():
        skill_id = int(raw_id)
        levels[skill_id] = ALL_V
        try:
            if dogma_data.has_type(skill_id) and \
                    not dogma_data.type_effects(skill_id):
                continue
        except (KeyError, ValueError, TypeError):
            pass                    # let _make_item report the broken row
        item = _make_item(skill_id, state=STATE_ACTIVE, quantity=1,
                          unmodeled=notes, seen=seen, category=CATEGORY_SKILL,
                          skill_gaps=gaps)
        if item is None:
            continue
        # Rule 5: the level lives on the skill item, in base as well as attrs.
        # base is what every clone starts from and what a fold reads for its
        # start value, so a level only in attrs would not survive either.
        item.base[ATTR_SKILL_LEVEL] = ALL_V
        item.attrs[ATTR_SKILL_LEVEL] = ALL_V
        item.skill_requirements()               # cache it once, for every fit
        item.resolved = _resolve_effects(item, {})
        prototypes.append(item)
    return tuple(prototypes), levels, frozenset(gaps), tuple(notes)


def _skill_prototypes():
    """The cached ``(prototypes, levels, gaps, notes)`` for the installed table.

    Rebuilt whenever the SDE build differs or a different table object has been
    installed (a re-seed in tests); the lock makes concurrent worker threads
    build it at most once.
    """
    global _SKILL_PROTOTYPES, _SKILL_PROTOTYPES_BUILD, _SKILL_PROTOTYPES_TABLE
    global _SKILL_LEVELS, _SKILL_GAPS, _SKILL_NOTES
    build = dogma_data.sde_build()
    table = getattr(dogma_data, "_table", None)
    with _PROTO_LOCK:
        if (_SKILL_PROTOTYPES is None or _SKILL_PROTOTYPES_BUILD != build
                or _SKILL_PROTOTYPES_TABLE is not table):
            prototypes, levels, gaps, notes = _build_skill_prototypes()
            _SKILL_PROTOTYPES = prototypes
            _SKILL_LEVELS = levels
            _SKILL_GAPS = gaps
            _SKILL_NOTES = notes
            _SKILL_PROTOTYPES_BUILD = build
            _SKILL_PROTOTYPES_TABLE = table
        return _SKILL_PROTOTYPES, _SKILL_LEVELS, _SKILL_GAPS, _SKILL_NOTES


def _reset_prototypes_for_tests() -> None:
    """Drop the prototype cache (and the table reference it pins).

    Tests that seed a second table through ``dogma_data._seed_for_tests`` are
    already covered by the identity check; this exists so a test can also prove
    the REBUILD path, and so no fixture table outlives its test."""
    global _SKILL_PROTOTYPES, _SKILL_PROTOTYPES_BUILD, _SKILL_PROTOTYPES_TABLE
    global _SKILL_LEVELS, _SKILL_GAPS, _SKILL_NOTES
    with _PROTO_LOCK:
        _SKILL_PROTOTYPES = None
        _SKILL_PROTOTYPES_BUILD = None
        _SKILL_PROTOTYPES_TABLE = None
        _SKILL_LEVELS = None
        _SKILL_GAPS = frozenset()
        _SKILL_NOTES = ()


def build_fit(parsed: ParsedFit, *, implants: Sequence[int] = ()) -> FitState:
    """Turn a parsed fit into an unevaluated :class:`FitState` at All-V skills.

    Rule 2: a module's charge becomes its own item, linked both ways
    (``module.charge`` / ``charge.parent``) so ``otherID`` can resolve either
    direction, and inheriting the module's state so an offline launcher's
    ammunition is silent too.

    ``implants`` are type ids the pilot is wearing; they become active,
    character-LOCATED items (rule 4), so a Mindlink's ``shipID`` modifiers reach
    the hull's modules exactly like a skill's do.  ``parsed.cargo`` is still
    ignored -- what is in the hold is not fitted.

    Rule 10: unknown or corrupt module / charge / drone / implant types are
    recorded in ``unmodeled`` and skipped.  An unusable HULL cannot be skipped --
    the fit would have no ship -- so it becomes an attribute-less stub, which
    evaluates to nothing rather than crashing the readout.

    Raises :class:`dogma_data.DogmaUnavailable` when no table is loaded.
    """
    unmodeled: list[str] = []
    seen: set = set()

    ship = _make_item(parsed.ship_type_id, state=STATE_ACTIVE, quantity=1,
                      unmodeled=unmodeled, seen=seen)
    if ship is None:
        ship = Item(parsed.ship_type_id, 0, CATEGORY_SHIP, {}, ())

    modules: list[Item] = []
    for parsed_module in parsed.modules:
        state = STATE_OFFLINE if parsed_module.offline else STATE_ACTIVE
        item = _make_item(parsed_module.type_id, state=state, quantity=1,
                          unmodeled=unmodeled, seen=seen)
        if item is None:
            continue
        if parsed_module.charge_type_id:
            charge = _make_item(parsed_module.charge_type_id, state=state,
                                quantity=1, unmodeled=unmodeled, seen=seen)
            if charge is not None:
                charge.parent = item
                item.charge = charge
        modules.append(item)

    drones: list[Item] = []
    for stack in parsed.drones:
        item = _make_item(stack.type_id, state=STATE_ACTIVE,
                          quantity=stack.quantity, unmodeled=unmodeled,
                          seen=seen)
        if item is not None:
            drones.append(item)

    implant_items: list[Item] = []
    for implant_id in (implants or ()):
        item = _make_item(implant_id, state=STATE_ACTIVE, quantity=1,
                          unmodeled=unmodeled, seen=seen)
        if item is not None:
            implant_items.append(item)

    for subsystem_id in (parsed.subsystems or ()):
        _record(unmodeled, seen, ("subsystem", subsystem_id),
                f"unmodeled subsystem {subsystem_id}")

    prototypes, levels, gaps, notes = _skill_prototypes()
    unmodeled.extend(notes)
    skill_items = [_clone_skill_item(p) for p in prototypes]

    character = Item(0, 0, CATEGORY_CHARACTER, {}, ())
    return FitState(ship=ship, modules=modules, drones=drones,
                    skills=dict(levels), unmodeled=unmodeled,
                    character=character, skill_items=skill_items,
                    implants=implant_items, unmodeled_keys=seen,
                    unmodeled_skill_effects=set(gaps))


# ===========================================================================
# evaluation
# ===========================================================================

def attr(item: Item, attr_id: int) -> float:
    """The item's current value for ``attr_id``, falling back to the dogma
    default when neither the type nor any modifier supplied one."""
    value = item.attrs.get(attr_id)
    if value is None:
        return dogma_data.attr_info(attr_id).default
    return value


def _skippable_modifier(modifier) -> bool:
    """Rule 10: whether this row is one the engine has no arithmetic for and
    must pass over WITHOUT reporting a modelling gap.

    Two shapes, both straight out of the SDE census (spec A.3): the ten
    ``EffectStopper`` rows, which carry no modified/modifying attribute and no
    ``operation`` at all, and the single ``operation`` 9 row on ``skillEffect``.
    The generator drops both, but the engine does not depend on that -- a table
    built by an older generator, or by hand, must degrade the same way.

    ``operation`` is compared by MEMBERSHIP so a ``None`` is skipped rather than
    raising.
    """
    return (modifier.func == FUNC_EFFECT_STOPPER
            or modifier.operation not in VALID_OPERATIONS)


class _Scope(NamedTuple):
    """The three item sets a modifier can reach, plus the skills their members
    require -- all fixed for the whole evaluation, so they are built once.

    The skill INDEXES are pure short-circuits: a required-skill modifier naming
    a skill nothing in that set requires reaches nothing, and answering that
    with one set membership instead of a scan over the location is what makes a
    fit's ~580 skills affordable (the overwhelming majority name a skill no
    item on this fit requires).
    """
    ship_location: list
    char_location: list
    owned: list
    ship_skills: frozenset
    char_skills: frozenset
    owned_skills: frozenset


def _required_skill_index(items) -> frozenset:
    """Every skill type id any of ``items`` requires."""
    skills: set = set()
    for item in items:
        skills.update(item.skill_requirements())
    return frozenset(skills)


def _scope(fit: FitState) -> _Scope:
    """Rule 4's locations for one evaluation."""
    ship_location = fit.ship_location()
    char_location = fit.char_location()
    owned = fit.owned_items()
    return _Scope(ship_location, char_location, owned,
                  _required_skill_index(ship_location),
                  _required_skill_index(char_location),
                  _required_skill_index(owned))


def _domain(modifier, source: Item, fit: FitState, scope: _Scope):
    """Rule 3: resolve a modifier's domain to ``(domain_item, located_items)``,
    or ``None`` when the domain is not modelled in v1.

    ``itemID`` is the effect-carrying item itself, and it is its OWN location:
    a ``LocationModifier`` on ``itemID`` reaches the carrier alone, never the
    hull's contents, even when the carrier happens to be the hull.
    ``otherID`` resolves to the source's counterpart -- a module's charge or a
    charge's launcher -- likewise its own one-item location.  ``targetID`` (no
    target exists) and ``structureID`` (no structure exists) are skipped.

    The locations come in already built (see :class:`_Scope`): they are the same
    lists for every modifier of an evaluation, and rebuilding them per modifier
    was measurable on a full skill table.
    """
    domain = modifier.domain
    if domain == DOMAIN_SHIP:
        return fit.ship, scope.ship_location
    if domain == DOMAIN_CHAR:
        return fit.character, scope.char_location
    if domain == DOMAIN_ITEM:
        return source, (source,)
    if domain == DOMAIN_OTHER:
        other = _other_item(source)
        if other is None:
            return None
        return other, (other,)
    return None


def _other_item(source: Item) -> Item | None:
    """Rule 2: a module's ``otherID`` is its charge; a charge's is its launcher."""
    if source.charge is not None:
        return source.charge
    return source.parent


def _required_skill(modifier, source: Item) -> int | None:
    """Rule 4: the skill a required-skill modifier filters on.

    :data:`SELF_SKILL` (-1) is the dogma self-reference: it means the effect's
    OWN carrier, so a skill's effect boosts exactly the items that require that
    skill without the table having to repeat its type id.
    """
    skill_id = modifier.skill_type_id
    if skill_id == SELF_SKILL:
        return source.type_id
    return skill_id


def _targets(modifier, source: Item, fit: FitState, scope: _Scope) -> list[Item]:
    """Rule 4: the items one modifier reaches.

    * ``ItemModifier`` -- the domain item itself.
    * ``LocationModifier`` -- everything in the domain's location (ship
      location = ship + modules + charges + drones; character location =
      skills + implants; ``itemID`` / ``otherID`` locations are the one
      resolved item).
    * ``LocationGroupModifier`` -- located items of the named group.
    * ``LocationRequiredSkillModifier`` -- located items requiring the skill.
    * ``OwnerRequiredSkillModifier`` -- character-OWNED items requiring the
      skill (drones and charges), regardless of location.

    An unknown func reaches nothing.
    """
    if modifier.func == FUNC_OWNER_SKILL:
        if modifier.domain in (DOMAIN_TARGET, DOMAIN_STRUCTURE):
            return []
        skill_id = _required_skill(modifier, source)
        if skill_id not in scope.owned_skills:
            return []
        return [i for i in scope.owned if skill_id in i.skill_requirements()]

    func = modifier.func
    #: The hot path -- a skill scaling its own per-level bonus attribute is
    #: ~half of a real fit's modifiers, and it needs no domain resolution: the
    #: itemID domain IS the source. Same answer as the general path below.
    if func == FUNC_ITEM and modifier.domain == DOMAIN_ITEM:
        return [source]

    resolved = _domain(modifier, source, fit, scope)
    if resolved is None:
        return []
    domain_item, located = resolved

    if func == FUNC_ITEM:
        return [] if domain_item is None else [domain_item]
    if func == FUNC_LOCATION:
        return list(located)
    if func == FUNC_LOCATION_GROUP:
        return [i for i in located if i.group_id == modifier.group_id]
    if func == FUNC_LOCATION_SKILL:
        skill_id = _required_skill(modifier, source)
        if located is scope.ship_location:
            if skill_id not in scope.ship_skills:
                return []
        elif located is scope.char_location:
            if skill_id not in scope.char_skills:
                return []
        return [i for i in located if skill_id in i.skill_requirements()]
    return []


def _penalised(modifier, source: Item) -> bool:
    """Rule 7: whether this contribution joins a stacking chain -- a
    multiplicative op, on a non-stackable attribute, from a module or drone,
    and not a raw skill-level read."""
    if modifier.operation not in MULTIPLICATIVE_OPS:
        return False
    if source.category_id not in PENALISED_SOURCE_CATEGORIES:
        return False
    if modifier.modifying_attr == ATTR_SKILL_LEVEL:
        return False
    return not dogma_data.attr_info(modifier.modified_attr).stackable


def _resolve_effects(source: Item, cache: dict):
    """Rule 1: the ``(EffectDef, applicable modifiers)`` pairs this source
    contributes in its current state.

    ``dogma_data.effect`` rebuilds NamedTuples on every call, so one cache per
    evaluation keeps the plan cheap; the cache remembers MISSES too (via
    ``_UNCACHED``), since an effect the table lacks would otherwise be re-probed
    on every item carrying it.  Rule 10's unparseable rows are filtered out
    here, once, rather than re-tested per pass.
    """
    allowed = _ALLOWED_CATEGORIES.get(source.state, frozenset())
    if not allowed:
        return ()
    out = []
    for effect_id in source.effects:
        entry = cache.get(effect_id, _UNCACHED)
        if entry is _UNCACHED:
            definition = dogma_data.effect(effect_id)
            if definition is None or not definition.modifiers:
                entry = None
            else:
                entry = (definition,
                         tuple(m for m in definition.modifiers
                               if not _skippable_modifier(m)))
            cache[effect_id] = entry
        if entry is None:
            continue
        if entry[0].category in allowed:
            out.append(entry)
    return tuple(out)


def _effect_defs(source: Item, cache: dict):
    """:func:`_resolve_effects`, short-circuited by the shared skill prototypes
    (which resolved their own effects once, for the whole table)."""
    if source.resolved is not None and source.state == STATE_ACTIVE:
        return source.resolved
    return _resolve_effects(source, cache)


class _Entry:
    """One (source, modifier) pair that reaches at least one item.

    ``value`` is the only thing a pass changes: the source's current reading of
    ``modifying_attr`` (rule 5).  A buff entry has no source and keeps the
    aggregated buff value it was built with (rule 9).

    ``dynamic`` says whether that reading can EVER change: it can only if some
    other contribution writes the very attribute this one reads, i.e. if
    ``(source, modifying_attr)`` is itself a group.  A static entry is read once
    for the whole evaluation, and a group built only from static entries folds
    to the same number every pass, so it is folded once too.  On a real fit that
    is most of the work: ~910 of 1,460 groups are a skill scaling its own
    per-level bonus attribute by its (fixed) level.
    """

    __slots__ = ("source", "modifying_attr", "default", "value", "dynamic")

    def __init__(self, source, modifying_attr, default, value=0.0):
        self.source = source
        self.modifying_attr = modifying_attr
        self.default = default
        self.value = value
        self.dynamic = False


class _Plan(NamedTuple):
    """One evaluation's resolved contribution graph (see :func:`_plan`).

    ``groups`` is every fold, in build order; ``dynamic_groups`` is the subset
    whose value can still move after the first pass.  Passes 2+ walk only those,
    and re-read only ``dynamic_entries``.
    """
    static_entries: list
    dynamic_entries: list
    groups: list
    dynamic_groups: list


class _Group:
    """Every contribution to one (item, attribute), grouped by operation and
    already sorted into :data:`OP_ORDER` -- rule 6's schedule, resolved once.

    ``ops`` is ``((operation, ((entry, penalised), ...)), ...)`` and ``start`` is
    the value every pass folds from -- the item's own base value, or the
    attribute's dogma default when it has none.
    """

    __slots__ = ("item", "attr_id", "start", "ops")

    def __init__(self, item, attr_id, start, ops):
        self.item = item
        self.attr_id = attr_id
        self.start = start
        self.ops = ops


def _record_effect_gap(fit: FitState, source: Item, effect_id: int) -> None:
    """An effect whose every modifier resolved to nothing (rule 3's "do not
    spam" clause).  Skill-sourced gaps go to the skill set, not the fit's
    user-facing list -- see the module docstring."""
    if source.category_id == CATEGORY_SKILL:
        fit.unmodeled_skill_effects.add((source.type_id, effect_id))
        return
    _record(fit.unmodeled, fit.unmodeled_keys,
            ("effect", source.type_id, effect_id),
            f"effect {effect_id} on type {source.type_id}")


def _plan(fit: FitState, buffs: Sequence[Buff], record_unmodeled: bool) -> _Plan:
    """Resolve the whole evaluation once.

    Source-backed contributions become :class:`_Entry` objects, split into the
    ones a pass has to re-read and the ones that can never move; the (item,
    attribute) folds become :class:`_Group` objects carrying the base value they
    start from.

    Rule 3's "do not spam" clause lives here: an effect whose modifiers ALL
    resolve to nothing (a pure ``targetID`` effect, say) is recorded once per
    type; an effect with at least one applicable modifier is not recorded at
    all, because it did something.  Rule 10's partial rows are invisible to that
    bookkeeping: a row the engine skips as unparseable is not a target it FAILED
    to reach, so an effect whose every row is skippable is neither applied nor
    reported.
    """
    scope = _scope(fit)
    effect_cache: dict = {}
    attr_defaults: dict = {}

    def _default(attr_id):
        value = attr_defaults.get(attr_id, _MISSING)
        if value is _MISSING:
            value = dogma_data.attr_info(attr_id).default
            attr_defaults[attr_id] = value
        return value

    entries: list[_Entry] = []
    by_read: dict = {}
    raw: dict = {}
    for source in fit.all_items():
        for definition, modifiers in _effect_defs(source, effect_cache):
            applied = False
            for modifier in modifiers:
                targets = _targets(modifier, source, fit, scope)
                if not targets:
                    continue
                applied = True
                # One entry per (source, attribute READ): what a modifier
                # carries depends only on those two, so rows that read the same
                # number share the read instead of repeating it every pass.
                read = (id(source), modifier.modifying_attr)
                entry = by_read.get(read)
                if entry is None:
                    entry = _Entry(source, modifier.modifying_attr,
                                   _default(modifier.modifying_attr))
                    by_read[read] = entry
                    entries.append(entry)
                member = (entry, _penalised(modifier, source))
                for target in targets:
                    per_op = raw.setdefault((target, modifier.modified_attr), {})
                    per_op.setdefault(modifier.operation, []).append(member)
            if modifiers and not applied and record_unmodeled:
                _record_effect_gap(fit, source, definition.effect_id)

    _buff_plan(fit, buffs, scope, raw, record_unmodeled)

    groups: list[_Group] = []
    dynamic_groups: list[_Group] = []
    group_keys = {(id(item), attr_id) for item, attr_id in raw}
    for read, entry in by_read.items():
        entry.dynamic = read in group_keys
    for (item, attr_id), per_op in raw.items():
        # Rule 6's schedule, resolved once: only the operations this group
        # actually carries, in dogma order.
        ops = tuple((operation, tuple(per_op[operation]))
                    for operation in sorted(per_op, key=_OP_RANK.__getitem__))
        # ``start`` is the value the fold begins from on EVERY pass: the item's
        # own base value, or the attribute's dogma default when it has none.
        # Reading it off ``base`` is what lets a pass skip resetting the live
        # store -- every attribute a pass writes is a group key, and every group
        # rewrites its key in full, so the reset it used to do was pure cost
        # (~600 dict copies per pass on a real fit).
        start = item.base.get(attr_id)
        if start is None:
            start = _default(attr_id)
        group = _Group(item, attr_id, start, ops)
        groups.append(group)
        for _operation, members in ops:
            if any(member[0].dynamic for member in members):
                dynamic_groups.append(group)
                break
    return _Plan([e for e in entries if not e.dynamic],
                 [e for e in entries if e.dynamic], groups, dynamic_groups)


def _buff_plan(fit: FitState, buffs: Sequence[Buff], scope: _Scope,
               raw: dict, record_unmodeled: bool) -> None:
    """Rule 9: fold fleet/warfare buffs into the same contribution table.

    Buff values aggregate per buff id first (``Maximum`` keeps the strongest,
    ``Minimum`` the weakest -- the dbuff's own declaration), then each
    ``BuffModifier`` applies with the dbuff's operation to matching
    ship-located items.  A buff's magnitude is an input, not a read, so its
    entry is built once with its final value.  Buff contributions are NEVER
    penalised: fleet boosts form their own group and do not join a module's
    stacking chain.
    """
    if not buffs:
        return
    strongest: dict[int, float] = {}
    for buff in buffs:
        definition = dogma_data.dbuff(buff.buff_id)
        if definition is None:
            if record_unmodeled:
                _record(fit.unmodeled, fit.unmodeled_keys,
                        ("buff", buff.buff_id), f"unknown buff {buff.buff_id}")
            continue
        current = strongest.get(buff.buff_id)
        if current is None:
            strongest[buff.buff_id] = buff.value
        elif definition.aggregate == "Minimum":
            strongest[buff.buff_id] = min(current, buff.value)
        else:
            strongest[buff.buff_id] = max(current, buff.value)

    for buff_id, value in strongest.items():
        definition = dogma_data.dbuff(buff_id)
        if definition is None:                          # pragma: no cover
            continue
        for buff_modifier in definition.modifiers:
            targets = _buff_targets(buff_modifier, fit, scope.ship_location)
            if not targets:
                continue
            member = (_Entry(None, buff_modifier.attr, 0.0, value), False)
            for target in targets:
                per_op = raw.setdefault((target, buff_modifier.attr), {})
                per_op.setdefault(definition.operation, []).append(member)


def _buff_targets(buff_modifier, fit: FitState, ship_location) -> list[Item]:
    """Rule 9: which ship-located items one dbuff modifier reaches."""
    kind = buff_modifier.kind
    if kind == BUFF_ITEM:
        return [fit.ship]
    if kind == BUFF_LOCATION:
        return list(ship_location)
    if kind == BUFF_LOCATION_GROUP:
        return [i for i in ship_location if i.group_id == buff_modifier.group_id]
    if kind == BUFF_LOCATION_SKILL:
        return [i for i in ship_location
                if buff_modifier.skill_type_id in i.skill_requirements()]
    return []


def _as_multiplier(operation: int, value: float) -> float:
    """Normalise a multiplicative contribution to a plain multiplier so the
    stacking chain can sort by |m - 1| across op kinds (rule 7).

    A division by zero is treated as a no-op rather than an error: a table
    defect must not take the readout down (rule 10)."""
    if operation == OP_POST_PERCENT:
        return 1.0 + value / 100.0
    if operation in (OP_PRE_DIV, OP_POST_DIV):
        return 1.0 / value if value else 1.0
    return value


def _penalised_product(multipliers: list[float]) -> float:
    """Rule 7: bonuses (> 1) and maluses (< 1) form SEPARATE chains, each
    sorted by |m - 1| descending, the i-th member contributing
    ``1 + (m - 1) * stacking_multiplier(i)``."""
    bonuses = sorted((m for m in multipliers if m > 1.0),
                     key=lambda m: -abs(m - 1.0))
    maluses = sorted((m for m in multipliers if m < 1.0),
                     key=lambda m: -abs(m - 1.0))
    product = 1.0
    for chain in (bonuses, maluses):
        for index, multiplier in enumerate(chain):
            product *= 1.0 + (multiplier - 1.0) * stacking_multiplier(index)
    return product


def _combined_multiplier(operation: int, members) -> float:
    """The single factor one multiplicative operation contributes: the
    unpenalised multipliers straight, the penalised ones through the chain."""
    plain = 1.0
    penalised: list[float] = []
    for entry, is_penalised in members:
        multiplier = _as_multiplier(operation, entry.value)
        if is_penalised:
            penalised.append(multiplier)
        else:
            plain *= multiplier
    if penalised:
        plain *= _penalised_product(penalised)
    return plain


def _apply(group: _Group, start: float) -> float:
    """Rule 6: fold one group's contributions in dogma operation order.

    ``preAssign -> preMul -> preDiv -> modAdd -> modSub -> postMul -> postDiv
    -> postPercent -> postAssign``.  ``postPercent p`` means x(1 + p/100);
    ``preDiv`` / ``postDiv`` divide; an assign takes the LAST value offered.
    """
    value = start
    for operation, members in group.ops:
        if operation in _ASSIGN_OPS:
            value = members[-1][0].value
        elif operation == OP_MOD_ADD:
            value += sum(member[0].value for member in members)
        elif operation == OP_MOD_SUB:
            value -= sum(member[0].value for member in members)
        else:
            value *= _combined_multiplier(operation, members)
    return value


def evaluate(fit: FitState, buffs: Sequence[Buff] = ()) -> FitState:
    """Apply every modifier (and any fleet ``buffs``) to ``fit``, in place.

    Rule 8 -- dependency ordering by fixed-point passes.  The contribution graph
    is resolved once (:func:`_plan`); each pass then reads every source value as
    the previous pass left it and re-folds every group from its ``base`` start
    (which is why no per-pass reset is needed: a fold rewrites its attribute in
    full).  Pass 1 therefore sees only base (and skill-level) values, pass 2
    sees pass 1's results, and so on.  The loop stops as soon as a pass changes
    nothing -- that confirming pass is not counted, and sets
    ``FitState.converged`` -- or at :data:`MAX_PASSES`, so a cyclic table
    terminates (with ``converged`` False) instead of hanging.
    ``FitState.passes`` records how many passes actually changed something.

    Idempotent: everything is rebuilt from ``base`` on entry, so evaluating
    twice yields the same values, the same pass count and the same verdict.
    """
    for item in fit.all_items():
        item.attrs = dict(item.base)
    fit.passes = 0
    fit.converged = False

    plan = _plan(fit, buffs, record_unmodeled=True)
    for entry in plan.static_entries:
        # Nothing writes what these read, so one read serves every pass.
        entry.value = entry.source.attrs.get(entry.modifying_attr,
                                             entry.default)
    groups = plan.groups

    for _pass_index in range(MAX_PASSES):
        # Every source value is read BEFORE any of this pass's writes, so the
        # fold sees the previous pass's fixed point, never a half-updated one.
        for entry in plan.dynamic_entries:
            entry.value = entry.source.attrs.get(entry.modifying_attr,
                                                 entry.default)
        changed = False
        for group in groups:
            attrs = group.item.attrs
            attr_id = group.attr_id
            value = _apply(group, group.start)
            before = attrs.get(attr_id, _MISSING)
            attrs[attr_id] = value
            if before is _MISSING or before != value:
                changed = True
        if not changed:
            fit.converged = True
            break
        fit.passes += 1
        # A group built only from static entries folded to its final value in
        # pass 1 and cannot move again; later passes walk the rest.
        groups = plan.dynamic_groups
    return fit
