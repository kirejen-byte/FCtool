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
