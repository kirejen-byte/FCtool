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
5. **Skill levels** -- :func:`_modifier_value`.
6. **Operation order** -- :func:`_reduce`.
7. **Stacking penalty** -- :func:`stacking_multiplier`, :func:`_penalised`,
   :func:`_combined_multiplier`.
8. **Dependency passes** -- :func:`evaluate`.
9. **Fleet buffs** -- :func:`_buff_contributions`.
10. **Never raise for data problems** -- :func:`_make_item`, :func:`_record`.

**Never loads anything.**  ``dogma_data.load()`` is the caller's business, on a
worker thread; every entry point here raises
:class:`dogma_data.DogmaUnavailable` if no table is installed.  That is the ONE
exception to rule 10: a missing table is a caller error, not fit data.

v1 simplifications, all deliberate and recorded in ``FitState.unmodeled`` where
they touch a specific fit:

* every fitted module is ``active`` unless the parser marked it ``offline`` --
  EFT/DNA carry no "online but not active" notion, so the engine has no way to
  produce ``STATE_ONLINE`` from a parsed fit (a caller may still set it on an
  :class:`Item` by hand, and the state machine honours it);
* overload (effect category 5) is never applied;
* ``targetID`` / ``structureID`` modifiers are skipped (no target is modelled);
* T3 subsystems and cargo (which is where the parser puts implants) are not
  evaluated -- subsystems are recorded as unmodeled, cargo is ignored;
* mutaplasmid rolls never reach here (the parser strips them).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import NamedTuple, Sequence

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

#: Rule 7: only module- and drone-sourced multipliers stack-penalise. Ships,
#: skills and charges are exempt.
PENALISED_SOURCE_CATEGORIES = frozenset({CATEGORY_MODULE, CATEGORY_DRONE})

# ── modifier domains (rule 3) ────────────────────────────────────────────────
DOMAIN_SHIP = "shipID"
DOMAIN_CHAR = "charID"
DOMAIN_OTHER = "otherID"
DOMAIN_TARGET = "targetID"
DOMAIN_STRUCTURE = "structureID"

# ── modifier funcs (rule 4) ──────────────────────────────────────────────────
FUNC_ITEM = "ItemModifier"
FUNC_LOCATION = "LocationModifier"
FUNC_LOCATION_GROUP = "LocationGroupModifier"
FUNC_LOCATION_SKILL = "LocationRequiredSkillModifier"
FUNC_OWNER_SKILL = "OwnerRequiredSkillModifier"

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


class Buff(NamedTuple):
    """One fleet/warfare buff to apply to a fit (rule 9)."""
    buff_id: int
    value: float


class Item:
    """One evaluated thing: the ship, a module, a charge, a drone stack, a
    skill, or the character.

    ``base`` is the type's own attribute values and is NEVER mutated after
    construction; ``attrs`` is the live value store the engine rewrites on every
    pass.  An attribute absent from both reads as its dogma default (see
    :func:`attr`).
    """

    __slots__ = ("type_id", "group_id", "category_id", "attrs", "base", "state",
                 "charge", "parent", "quantity", "effects", "unmodeled_effects",
                 "_skill_reqs")

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
    passes: int = 0                           # rule 8, recorded for the tests
    unmodeled_keys: set = field(default_factory=set)

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
        """Rule 4: the character location holds the skills (and, from v2, the
        implants).  The ship and its contents are NOT character-located."""
        return list(self.skill_items)

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


def _make_item(type_id, *, state, quantity, unmodeled, seen, category=None):
    """Build one :class:`Item` from the table, or None when the type is unknown.

    Rule 10: an unknown type is recorded and skipped -- never raised.  Effects
    the table cannot model (absent, or present with no modifiers) are recorded
    once per type and remembered on ``Item.unmodeled_effects``.
    """
    if not dogma_data.has_type(type_id):
        _record(unmodeled, seen, ("type", type_id), f"unknown type {type_id}")
        return None
    attrs = dogma_data.type_attrs(type_id)
    effect_ids = dogma_data.type_effects(type_id)
    unmodeled_effects = []
    for effect_id in effect_ids:
        definition = dogma_data.effect(effect_id)
        if definition is None or not definition.modifiers:
            unmodeled_effects.append(effect_id)
            _record(unmodeled, seen, ("effect", type_id, effect_id),
                    f"effect {effect_id} on type {type_id}")
    return Item(type_id, dogma_data.type_group(type_id),
                dogma_data.type_category(type_id) if category is None
                else category,
                attrs, effect_ids, state=state, quantity=quantity,
                unmodeled_effects=unmodeled_effects)


def build_fit(parsed: ParsedFit) -> FitState:
    """Turn a parsed fit into an unevaluated :class:`FitState` at All-V skills.

    Rule 2: a module's charge becomes its own item, linked both ways
    (``module.charge`` / ``charge.parent``) so ``otherID`` can resolve either
    direction, and inheriting the module's state so an offline launcher's
    ammunition is silent too.

    Rule 10: unknown hull / module / charge / drone types are recorded in
    ``unmodeled`` and skipped.  An unknown HULL cannot be skipped -- the fit
    would have no ship -- so it becomes an attribute-less stub, which evaluates
    to nothing rather than crashing the readout.

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

    for subsystem_id in (parsed.subsystems or ()):
        _record(unmodeled, seen, ("subsystem", subsystem_id),
                f"unmodeled subsystem {subsystem_id}")

    skills = {int(sid): ALL_V for sid in dogma_data.skill_type_ids()}
    skill_items: list[Item] = []
    for skill_id in skills:
        item = _make_item(skill_id, state=STATE_ACTIVE, quantity=1,
                          unmodeled=unmodeled, seen=seen,
                          category=CATEGORY_SKILL)
        if item is None:
            continue
        # Rule 5: the level lives on the skill item, in base as well as attrs,
        # so a pass reset cannot wipe it.
        item.base[ATTR_SKILL_LEVEL] = ALL_V
        item.attrs[ATTR_SKILL_LEVEL] = ALL_V
        skill_items.append(item)

    character = Item(0, 0, CATEGORY_CHARACTER, {}, ())
    return FitState(ship=ship, modules=modules, drones=drones, skills=skills,
                    unmodeled=unmodeled, character=character,
                    skill_items=skill_items, unmodeled_keys=seen)


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


def _domain(modifier, source: Item, fit: FitState):
    """Rule 3: resolve a modifier's domain to ``(domain_item, located_items)``,
    or ``None`` when the domain is not modelled in v1.

    ``targetID`` (no target exists) and ``structureID`` (no structure exists)
    are skipped.  ``otherID`` resolves to the source's counterpart -- a module's
    charge or a charge's launcher -- which is its own one-item location.
    """
    domain = modifier.domain
    if domain == DOMAIN_SHIP:
        return fit.ship, fit.ship_location()
    if domain == DOMAIN_CHAR:
        return fit.character, fit.char_location()
    if domain == DOMAIN_OTHER:
        other = _other_item(source)
        if other is None:
            return None
        return other, [other]
    return None


def _other_item(source: Item) -> Item | None:
    """Rule 2: a module's ``otherID`` is its charge; a charge's is its launcher."""
    if source.charge is not None:
        return source.charge
    return source.parent


def _targets(modifier, source: Item, fit: FitState,
             ship_location, char_location, owned) -> list[Item]:
    """Rule 4: the items one modifier reaches.

    * ``ItemModifier`` -- the domain item itself.
    * ``LocationModifier`` -- everything in the domain's location (ship
      location = ship + modules + charges + drones; character location =
      skills).
    * ``LocationGroupModifier`` -- located items of the named group.
    * ``LocationRequiredSkillModifier`` -- located items requiring the skill.
    * ``OwnerRequiredSkillModifier`` -- character-OWNED items requiring the
      skill (drones and charges), regardless of location.

    An unknown func reaches nothing.
    """
    if modifier.func == FUNC_OWNER_SKILL:
        if modifier.domain in (DOMAIN_TARGET, DOMAIN_STRUCTURE):
            return []
        return [i for i in owned
                if modifier.skill_type_id in i.skill_requirements()]

    resolved = _domain(modifier, source, fit)
    if resolved is None:
        return []
    domain_item, located = resolved
    if domain_item is fit.ship:
        located = ship_location
    elif domain_item is fit.character:
        located = char_location

    func = modifier.func
    if func == FUNC_ITEM:
        return [] if domain_item is None else [domain_item]
    if func == FUNC_LOCATION:
        return list(located)
    if func == FUNC_LOCATION_GROUP:
        return [i for i in located if i.group_id == modifier.group_id]
    if func == FUNC_LOCATION_SKILL:
        return [i for i in located
                if modifier.skill_type_id in i.skill_requirements()]
    return []


def _modifier_value(modifier, source: Item, source_values: dict) -> float:
    """Rule 5: the magnitude a modifier carries this pass.

    The raw value is the SOURCE's current reading of ``modifying_attr`` (from
    the previous pass's snapshot, so a chain converges instead of depending on
    iteration order).

    Skill-sourced modifiers scale with the skill's level: a skill whose bonus
    attribute reads 5 ("+5 %/level") contributes 25 at All V.  The one exception
    is a modifier whose modifying attribute IS ``ATTR_SKILL_LEVEL`` -- that
    value already is the level and must not be squared.
    """
    values = source_values.get(id(source)) or source.attrs
    raw = values.get(modifier.modifying_attr)
    if raw is None:
        raw = dogma_data.attr_info(modifier.modifying_attr).default
    if source.category_id == CATEGORY_SKILL and \
            modifier.modifying_attr != ATTR_SKILL_LEVEL:
        level = values.get(ATTR_SKILL_LEVEL)
        if level is None:
            level = ALL_V
        raw *= level
    return raw


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


def _effect_defs(source: Item, cache: dict):
    """Rule 1: the effects this source contributes right now, filtered by its
    state.  ``dogma_data.effect`` rebuilds NamedTuples on every call, so one
    cache per evaluation keeps the passes cheap."""
    allowed = _ALLOWED_CATEGORIES.get(source.state, frozenset())
    if not allowed:
        return ()
    out = []
    for effect_id in source.effects:
        definition = cache.get(effect_id)
        if definition is None:
            definition = dogma_data.effect(effect_id)
            cache[effect_id] = definition
        if definition is None or not definition.modifiers:
            continue
        if definition.category in allowed:
            out.append(definition)
    return out


def _collect(fit: FitState, source_values: dict, effect_cache: dict,
             record_unmodeled: bool) -> dict:
    """Gather every contribution of one pass as
    ``{(item, attr_id): {operation: [(value, penalised), ...]}}``.

    Rule 3's "do not spam" clause lives here: an effect whose modifiers ALL
    resolve to nothing (a pure ``targetID`` effect, say) is recorded once per
    type; an effect with at least one applicable modifier is not recorded at
    all, because it did something.
    """
    contributions: dict = {}
    ship_location = fit.ship_location()
    char_location = fit.char_location()
    owned = fit.owned_items()

    for source in fit.all_items():
        for definition in _effect_defs(source, effect_cache):
            applied = False
            for modifier in definition.modifiers:
                targets = _targets(modifier, source, fit, ship_location,
                                   char_location, owned)
                if not targets:
                    continue
                applied = True
                value = _modifier_value(modifier, source, source_values)
                penalised = _penalised(modifier, source)
                for target in targets:
                    key = (target, modifier.modified_attr)
                    per_op = contributions.setdefault(key, {})
                    per_op.setdefault(modifier.operation, []).append(
                        (value, penalised))
            if not applied and record_unmodeled:
                _record(fit.unmodeled, fit.unmodeled_keys,
                        ("effect", source.type_id, definition.effect_id),
                        f"effect {definition.effect_id} on type "
                        f"{source.type_id}")
    return contributions


def _buff_contributions(fit: FitState, buffs: Sequence[Buff],
                        contributions: dict, record_unmodeled: bool) -> None:
    """Rule 9: fold fleet/warfare buffs into the same contribution table.

    Buff values aggregate per buff id first (``Maximum`` keeps the strongest,
    ``Minimum`` the weakest -- the dbuff's own declaration), then each
    ``BuffModifier`` applies with the dbuff's operation to matching
    ship-located items.  Buff contributions are NEVER penalised: fleet boosts
    form their own group and do not join a module's stacking chain.
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

    ship_location = fit.ship_location()
    for buff_id, value in strongest.items():
        definition = dogma_data.dbuff(buff_id)
        if definition is None:                          # pragma: no cover
            continue
        for buff_modifier in definition.modifiers:
            for target in _buff_targets(buff_modifier, fit, ship_location):
                key = (target, buff_modifier.attr)
                per_op = contributions.setdefault(key, {})
                per_op.setdefault(definition.operation, []).append(
                    (value, False))


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


def _combined_multiplier(operation: int, entries) -> float:
    """The single factor one multiplicative operation contributes: the
    unpenalised multipliers straight, the penalised ones through the chain."""
    plain = 1.0
    penalised: list[float] = []
    for value, is_penalised in entries:
        multiplier = _as_multiplier(operation, value)
        if is_penalised:
            penalised.append(multiplier)
        else:
            plain *= multiplier
    if penalised:
        plain *= _penalised_product(penalised)
    return plain


def _reduce(contributions: dict) -> None:
    """Rule 6: apply every contribution in dogma operation order, in place.

    ``preAssign -> preMul -> preDiv -> modAdd -> modSub -> postMul -> postDiv
    -> postPercent -> postAssign``.  ``postPercent p`` means x(1 + p/100);
    ``preDiv`` / ``postDiv`` divide; an assign takes the LAST value offered.
    """
    for (item, attr_id), per_op in contributions.items():
        value = item.attrs.get(attr_id)
        if value is None:
            value = dogma_data.attr_info(attr_id).default
        for operation in OP_ORDER:
            entries = per_op.get(operation)
            if not entries:
                continue
            if operation in _ASSIGN_OPS:
                value = entries[-1][0]
            elif operation == OP_MOD_ADD:
                value += sum(entry[0] for entry in entries)
            elif operation == OP_MOD_SUB:
                value -= sum(entry[0] for entry in entries)
            else:
                value *= _combined_multiplier(operation, entries)
        item.attrs[attr_id] = value


def evaluate(fit: FitState, buffs: Sequence[Buff] = ()) -> FitState:
    """Apply every modifier (and any fleet ``buffs``) to ``fit``, in place.

    Rule 8 -- dependency ordering by fixed-point passes.  Each pass snapshots
    the current values, resets every item to its ``base``, then recollects and
    reapplies every contribution reading modifying attributes from the
    snapshot.  Pass 1 therefore sees only base (and skill-level) values, pass 2
    sees pass 1's results, and so on.  The loop stops as soon as a pass changes
    nothing -- that confirming pass is not counted -- or at
    :data:`MAX_PASSES`, so a cyclic table terminates instead of hanging.
    ``FitState.passes`` records how many passes actually changed something.

    Idempotent: everything is rebuilt from ``base`` on entry, so evaluating
    twice yields the same values and the same pass count.
    """
    items = fit.all_items()
    for item in items:
        item.attrs = dict(item.base)
    fit.passes = 0
    effect_cache: dict = {}

    for pass_index in range(MAX_PASSES):
        source_values = {id(item): dict(item.attrs) for item in items}
        for item in items:
            item.attrs = dict(item.base)
        contributions = _collect(fit, source_values, effect_cache,
                                 record_unmodeled=(pass_index == 0))
        _buff_contributions(fit, buffs, contributions,
                            record_unmodeled=(pass_index == 0))
        _reduce(contributions)
        if all(item.attrs == source_values[id(item)] for item in items):
            break
        fit.passes += 1
    return fit
