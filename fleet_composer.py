# fleet_composer.py
"""Fleet composition matching — pure logic, no Tk, no ESI, no network.

`compose(template, live_members, live_structure, ...)` assigns live pilots to
the template's slots in precedence order — named slots, then rule-driven role
slots, then generic slots, then Unassigned — diffs each assignment against the
pilot's current ESI placement, and returns a `ComposeResult` whose executable
moves (`skip_reason is None`) are exactly the ESI writes the apply step issues.

`plan_rebalance(...)` returns a single overflow move for the size-cap rebalancer
(at most one per tick, by design).

All targets are expressed by wing/squad NAME; the caller resolves names to live
ESI ids (creating wings/squads as needed) at apply time.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from fleet_template_store import AssignmentRule, RuleCondition, RuleAction

# Lowest-priority sentinel so an implicit tag-rule never outranks a user rule.
_IMPLICIT_PRIORITY = 1_000_000


@dataclass
class Move:
    pilot_id: int
    pilot_name: str
    target_wing_name: str | None
    target_squad_name: str | None
    target_role: str
    skip_reason: str | None = None   # None => executable ESI write; else informational


@dataclass
class ComposeResult:
    moves: list[Move] = field(default_factory=list)        # all (incl. already-correct)
    unassigned: list[dict] = field(default_factory=list)   # enriched member dicts
    warnings: list[str] = field(default_factory=list)

    @property
    def executable(self) -> list[Move]:
        return [m for m in self.moves if m.skip_reason is None]


def build_tag_index(doctrine, fittings) -> dict[int, set[str]]:
    """ship_type_id → union of doctrine tags whose fit uses that hull.

    Empty dict when no doctrine/fittings (so doctrine_tag rules never fire —
    the "no doctrine active" inactive state)."""
    index: dict[int, set[str]] = {}
    if not doctrine or not fittings:
        return index
    for member in getattr(doctrine, "members", []):
        fit = fittings.get_fit(member.fit_id)
        if fit is None:
            continue
        index.setdefault(fit.hull_type_id, set()).update(member.tags)
    return index


# append to fleet_composer.py
def _pilot_matches(condition, member, tag_index) -> bool:
    t, v = condition.type, condition.value
    if t == "character":
        return (member.get("name") or "").lower() == v.lower()
    if t == "ship_type":
        return (member.get("ship_type_name") or "").lower() == v.lower()
    if t == "ship_class":
        # Pre-resolved off the Tk thread in _enrich_members → no network here.
        return (member.get("ship_class") or "").lower() == v.lower()
    if t == "doctrine_tag":
        return v in tag_index.get(member.get("ship_type_id"), set())
    return False


def _current_placement(member, id_to_names):
    """(wing_name, squad_name, role) for a member's current ESI position, or
    (None, None, role) if its wing/squad id isn't in the known structure."""
    key = (member.get("wing_id"), member.get("squad_id"))
    wname, sname = id_to_names.get(key, (None, None))
    return wname, sname, member.get("role")


def compose(template, live_members, live_structure, *, doctrine=None,
            fittings=None) -> ComposeResult:
    """Assign pilots to template slots and diff against current placement.

    See module docstring for the member/structure dict shapes. Ship-class rule
    conditions read each member's pre-resolved `ship_class` field (resolved off
    the Tk thread in the window's _enrich_members), so compose stays
    network-free and safe to call on the UI thread.
    """
    result = ComposeResult()
    tag_index = build_tag_index(doctrine, fittings)

    # id → names for the diff step.
    id_to_names: dict[tuple, tuple] = {}
    for w in live_structure.get("wings", []):
        for s in w.get("squads", []):
            id_to_names[(w["id"], s["id"])] = (w["name"], s["name"])

    # Pool ordered by join_time ascending (longest-serving first). ESI join_time
    # is an ISO8601 string; lexical sort matches chronological order.
    pool = sorted(live_members, key=lambda m: m.get("join_time") or "")
    claimed: set = set()
    assignment: dict = {}   # character_id → (wing_name, squad_name, role)

    # Flatten slots in tree order.
    flat = [(w.name, s.name, slot)
            for w in template.wings for s in w.squads for slot in s.slots]

    by_name: dict[str, list] = {}
    for m in pool:
        by_name.setdefault((m.get("name") or "").lower(), []).append(m)

    # Pass 1 — named slots.
    for wname, sname, slot in flat:
        if not slot.character:
            continue
        cand = next((c for c in by_name.get(slot.character.lower(), [])
                     if c["character_id"] not in claimed), None)
        if cand is not None:
            assignment[cand["character_id"]] = (wname, sname, slot.role)
            claimed.add(cand["character_id"])

    # Pass 2 — rule-driven role slots (slot.tag set, slot.character None).
    user_rules = sorted((r for r in template.rules if not r.broken),
                        key=lambda r: r.priority)
    for wname, sname, slot in flat:
        if slot.character or slot.tag is None:
            continue
        candidate_rules = [
            r for r in user_rules
            if r.action.role == slot.role
            and r.action.wing_name in (None, wname)
            and r.action.squad_name in (None, sname)
        ]
        # Implicit lowest-priority rule from the slot's own doctrine tag.
        candidate_rules.append(AssignmentRule(
            _IMPLICIT_PRIORITY,
            RuleCondition("doctrine_tag", slot.tag),
            RuleAction(slot.role, wname, sname)))
        for rule in candidate_rules:
            pilot = next((m for m in pool if m["character_id"] not in claimed
                          and _pilot_matches(rule.condition, m, tag_index)), None)
            if pilot is not None:
                assignment[pilot["character_id"]] = (wname, sname, slot.role)
                claimed.add(pilot["character_id"])
                break
        else:
            result.warnings.append(
                f"1 slot unfilled (no match): {wname}/{sname} [{slot.tag}]")

    # Pass 2b — warn about pilots a user rule matched but had no open slot for.
    for rule in user_rules:
        leftover = [m for m in pool if m["character_id"] not in claimed
                    and _pilot_matches(rule.condition, m, tag_index)]
        if leftover:
            result.warnings.append(
                f"{len(leftover)} {rule.condition.value} unplaced by "
                f"{rule.action.role} rule (no open slot).")

    # Pass 3 — generic slots (character None, tag None), tree order.
    for wname, sname, slot in flat:
        if slot.character or slot.tag is not None:
            continue
        pilot = next((m for m in pool if m["character_id"] not in claimed), None)
        if pilot is not None:
            assignment[pilot["character_id"]] = (wname, sname, slot.role)
            claimed.add(pilot["character_id"])

    # Build moves (diff each assignment vs current placement).
    member_by_id = {m["character_id"]: m for m in pool}
    for cid, (wname, sname, role) in assignment.items():
        m = member_by_id[cid]
        cur = _current_placement(m, id_to_names)
        skip = "already_correct" if cur == (wname, sname, role) else None
        result.moves.append(Move(
            pilot_id=cid, pilot_name=m.get("name", ""),
            target_wing_name=wname, target_squad_name=sname,
            target_role=role, skip_reason=skip))

    # Pass 5 — leftover pool → Unassigned.
    result.unassigned = [m for m in pool if m["character_id"] not in claimed]
    return result


# append to fleet_composer.py
def summarize_moves(result: ComposeResult) -> dict:
    """Counts for the apply confirm dialog. `esi_calls` is the move count;
    the apply layer adds wing/squad creates on top, so this is a lower bound."""
    executable = len(result.executable)
    unfilled = sum(1 for w in result.warnings if "unfilled" in w)
    return {
        "executable": executable,
        "unfilled": unfilled,
        "unassigned": len(result.unassigned),
        "esi_calls": executable,
    }


# append to fleet_composer.py
@dataclass
class RebalanceAction:
    pilot_id: int
    pilot_name: str
    source_wing_name: str
    target_wing_name: str
    target_squad_name: str | None   # None + create_squad=True → make a new squad here
    create_squad: bool = False


def plan_rebalance(live_members, live_structure, *, max_sizes) -> "RebalanceAction | None":
    """Return at most ONE overflow move, or None if every squad is within cap.

    Order of preference for the target (spec §9): an under-cap squad in the SAME
    wing → the least-populated squad across all wings → signal create_squad in
    the same wing. The overflow pilot is the last-joined in the over-cap squad.
    `max_sizes`: {(wing_name, squad_name): cap or None}. None means uncapped.

    NOTE: `RebalanceSettings.overflow_strategy` is reserved for v1. The only
    documented/default value is "least_populated", which is exactly this
    target-selection order, so the field is persisted (round-tripped) but not
    branched on yet and is intentionally not exposed in the Settings tab. A
    future strategy (e.g. "fill_first") would add a branch here.
    """
    # Index structure.
    wings = live_structure.get("wings", [])
    sid_to_names: dict[int, tuple] = {}
    wname_by_id: dict[int, str] = {}
    squads_by_wing: dict[str, list] = {}
    for w in wings:
        wname_by_id[w["id"]] = w["name"]
        squads_by_wing[w["name"]] = []
        for s in w.get("squads", []):
            sid_to_names[s["id"]] = (w["name"], s["name"])
            squads_by_wing[w["name"]].append(s["name"])

    # Population per (wing_name, squad_name).
    pop: dict[tuple, list] = {}
    for m in live_members:
        names = sid_to_names.get(m.get("squad_id"))
        if names:
            pop.setdefault(names, []).append(m)

    # First over-cap squad in tree order.
    for w in wings:
        for s in w.get("squads", []):
            key = (w["name"], s["name"])
            cap = max_sizes.get(key)
            members = pop.get(key, [])
            if cap is None or len(members) <= cap:
                continue
            overflow = max(members, key=lambda m: m.get("join_time") or "")

            # (i) under-cap squad in the same wing
            same_wing = [(w["name"], sn) for sn in squads_by_wing[w["name"]]
                         if sn != s["name"]]
            under = [k for k in same_wing
                     if max_sizes.get(k) is None or len(pop.get(k, [])) < max_sizes[k]]
            if under:
                target = min(under, key=lambda k: len(pop.get(k, [])))
                return RebalanceAction(overflow["character_id"],
                                       overflow.get("name", ""), w["name"],
                                       target[0], target[1], create_squad=False)

            # (ii) least-populated under-cap squad across all wings
            all_other = [k for k in pop.keys() | set(max_sizes.keys())
                         if k != key]
            under_any = [k for k in all_other
                         if max_sizes.get(k) is None or len(pop.get(k, [])) < max_sizes[k]]
            if under_any:
                target = min(under_any, key=lambda k: len(pop.get(k, [])))
                return RebalanceAction(overflow["character_id"],
                                       overflow.get("name", ""), w["name"],
                                       target[0], target[1], create_squad=False)

            # (iii) no under-cap squad anywhere → create one in the same wing
            return RebalanceAction(overflow["character_id"], overflow.get("name", ""),
                                   w["name"], w["name"], None, create_squad=True)

    return None
