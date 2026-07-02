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

_NAME_MAX = 10


def _clamp(name):
    return name[:_NAME_MAX] if isinstance(name, str) else name


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
    unassigned_reasons: dict = field(default_factory=dict)  # character_id -> reason
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
    if t == "capital":
        return member.get("is_capital") is True
    if t == "subcap":
        return member.get("is_capital") is False and bool(member.get("ship_class"))
    if t == "default":
        return True
    return False


def _current_placement(member, id_to_names):
    """(wing_name, squad_name, role) for a member's current ESI position, or
    (None, None, role) if its wing/squad id isn't in the known structure."""
    key = (member.get("wing_id"), member.get("squad_id"))
    wname, sname = id_to_names.get(key, (None, None))
    return wname, sname, member.get("role")


def compose(template, live_members, live_structure, *, doctrine=None,
            fittings=None, passes=(1, 2, 3)) -> ComposeResult:
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

    # id → member, for id-preferred named matching.
    by_id = {m["character_id"]: m for m in pool}

    # Per-(wing, squad) count of pilots THIS compose assigns into that squad, in
    # ANY pass. Seeded by Pass 1 so Pass 2's cap-respect counts named pins too.
    assigned_here: dict[tuple, int] = {}

    # Pass 1 — named slots (prefer character_id, fall back to name).
    if 1 in passes:
        for wname, sname, slot in flat:
            if not slot.character and getattr(slot, "character_id", None) is None:
                continue
            cand = None
            sid = getattr(slot, "character_id", None)
            if sid is not None:
                m = by_id.get(sid)
                if m is not None and m["character_id"] not in claimed:
                    cand = m
            if cand is None and slot.character:
                cand = next((c for c in by_name.get(slot.character.lower(), [])
                             if c["character_id"] not in claimed), None)
            if cand is not None:
                assignment[cand["character_id"]] = (wname, sname, slot.role)
                claimed.add(cand["character_id"])
                assigned_here[(wname, sname)] = assigned_here.get((wname, sname), 0) + 1

    # Pass 2 — routing rules assign DIRECTLY to a single wing+squad+role, in
    # priority order (default-type rules forced last). No tagged slots.
    if 2 in passes:
        # Squad max_size lookup by (wing, squad) name.
        squad_caps: dict[tuple, int | None] = {}
        for w in template.wings:
            for sq in w.squads:
                squad_caps[(w.name, sq.name)] = sq.max_size

        # Occupants who count against a squad's cap = pool pilots PHYSICALLY in
        # that squad right now who are NOT being reassigned somewhere else this
        # compose (they "stay"). A pilot pinned INTO this squad by Pass 1 is NOT
        # counted here (they may not be physically in it yet) — instead Pass 1
        # already recorded them in `assigned_here[key]`, and the cap check below
        # adds `assigned_here[key]` on top of this base. That split keeps every
        # occupant counted exactly once: physical-and-staying via base, and
        # assigned-this-compose (Pass 1 pins + Pass 2 routes) via assigned_here.
        def _staying_count(wname, sname):
            n = 0
            for m in pool:
                cur_w, cur_s, _ = _current_placement(m, id_to_names)
                if (cur_w, cur_s) == (wname, sname) \
                        and m["character_id"] not in assignment:
                    n += 1
            return n

        ordered = sorted(
            (r for r in template.rules if not r.broken),
            key=lambda r: (r.condition.type == "default", r.priority))
        for rule in ordered:
            wname = rule.action.wing_name
            sname = rule.action.squad_name
            if wname is None or sname is None:
                continue   # single-squad targets only
            key = (wname, sname)
            cap = squad_caps.get(key)
            base = _staying_count(wname, sname) if cap is not None else 0
            for m in pool:
                if m["character_id"] in claimed:
                    continue
                if not _pilot_matches(rule.condition, m, tag_index):
                    continue
                if cap is not None:
                    # base (physical stayers) + every pilot this compose has
                    # already assigned into the squad (Pass 1 pins + earlier
                    # Pass 2 routes) — so a named pin cannot be overfilled past.
                    used = base + assigned_here.get(key, 0)
                    if used >= cap:
                        result.unassigned_reasons[m["character_id"]] = "target full"
                        continue
                assignment[m["character_id"]] = (wname, sname, rule.action.role)
                claimed.add(m["character_id"])
                assigned_here[key] = assigned_here.get(key, 0) + 1

        # Pass-2 tail — warn about routing rules that matched NO pilot at all, so
        # a rule pointing at an absent ship class/tag surfaces in the preview
        # (replaces the old tagged-slot "unfilled" warnings). "target full"
        # blocks don't count as a match here, so an all-full rule still warns
        # only if it placed nobody. Uses the rule's condition value (or, for the
        # valueless capital/subcap/default types, the condition type).
        for rule in ordered:
            key = (rule.action.wing_name, rule.action.squad_name)
            if key[0] is None or key[1] is None:
                continue
            placed_any = any(
                _pilot_matches(rule.condition, m, tag_index)
                and assignment.get(m["character_id"]) == (key[0], key[1], rule.action.role)
                for m in pool)
            if not placed_any:
                label = rule.condition.value or rule.condition.type
                result.warnings.append(f"Rule {label}: no matching pilots")

    # Pass 3 — generic slots (character None, tag None), tree order.
    if 3 in passes:
        for wname, sname, slot in flat:
            if slot.character or slot.tag is not None \
                    or getattr(slot, "character_id", None) is not None:
                continue
            pilot = next((m for m in pool if m["character_id"] not in claimed), None)
            if pilot is not None:
                assignment[pilot["character_id"]] = (wname, sname, slot.role)
                claimed.add(pilot["character_id"])

    # Build moves (diff each assignment vs current placement).
    member_by_id = {m["character_id"]: m for m in pool}
    for cid, (wname, sname, role) in assignment.items():
        m = member_by_id[cid]
        cur_w, cur_s, cur_role = _current_placement(m, id_to_names)
        same = (cur_role == role
                and _clamp(cur_w) == _clamp(wname)
                and _clamp(cur_s) == _clamp(sname))
        skip = "already_correct" if same else None
        result.moves.append(Move(
            pilot_id=cid, pilot_name=m.get("name", ""),
            target_wing_name=wname, target_squad_name=sname,
            target_role=role, skip_reason=skip))

    # Leftover pool + cap-overflow → Unassigned (de-duped; overflow keeps its reason).
    seen_unassigned: set = set()
    result.unassigned = []
    for m in pool:
        cid = m["character_id"]
        if cid in claimed or cid in seen_unassigned:
            continue
        seen_unassigned.add(cid)
        result.unassigned.append(m)
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


def live_layout(live_members, live_structure):
    """Map live members onto the actual fleet structure for Live-mode rendering.

    Returns {"fc": member|None, "wings": [...], "unplaced": [member, ...]} where
    each wing is {"id","name","wc": member|None, "squads": [...]} and each squad is
    {"id","name","sc": member|None, "members": [member, ...]}. A member is "placed"
    if it is the fleet_commander, a wing_commander of a known wing, a squad_commander
    of a known squad, or sits in a known squad; everything else is "unplaced"
    (e.g. a just-joined pilot in EVE's no-squad slot). Matching is by id, robust to
    ESI's -1 / None "no wing/squad" sentinels (they simply aren't in the id sets).
    """
    valid_wings = {w["id"] for w in live_structure.get("wings", [])}
    valid_squads = {s["id"] for w in live_structure.get("wings", [])
                    for s in w.get("squads", [])}
    fc = None
    wcs: dict = {}      # wing_id -> member
    scs: dict = {}      # squad_id -> member
    by_squad: dict = {}  # squad_id -> [member, ...]
    placed: set = set()
    for m in live_members:
        cid = m.get("character_id")
        role = m.get("role")
        wid = m.get("wing_id")
        sid = m.get("squad_id")
        if role == "fleet_commander":
            fc = m
            placed.add(cid)
        if role == "wing_commander" and wid in valid_wings:
            wcs[wid] = m
            placed.add(cid)
        if role == "squad_commander" and sid in valid_squads:
            scs[sid] = m
            placed.add(cid)
        if sid in valid_squads:
            by_squad.setdefault(sid, []).append(m)
            placed.add(cid)
    wings_out = []
    for w in live_structure.get("wings", []):
        squads_out = []
        for s in w.get("squads", []):
            squads_out.append({
                "id": s["id"], "name": s["name"],
                "sc": scs.get(s["id"]),
                "members": by_squad.get(s["id"], []),
            })
        wings_out.append({
            "id": w["id"], "name": w["name"],
            "wc": wcs.get(w["id"]),
            "squads": squads_out,
        })
    unplaced = [m for m in live_members if m.get("character_id") not in placed]
    return {"fc": fc, "wings": wings_out, "unplaced": unplaced}
