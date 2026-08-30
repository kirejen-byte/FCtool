# cycle_roles.py
"""Cycle-group ROLES — the pure criteria vocabulary and ring resolution.

A preview cycle group is either `mode == "chars"` (its manual member list —
today's behaviour, byte-identical) or `mode == "roles"`, where the ring is
COMPUTED at every hotkey press from the live clients' CURRENT ships. This
module owns that computation and nothing else: no Tk, no ESI, no network, no
disk, no threads.

Design: ``docs/superpowers/specs/2026-08-29-preview-settings-categories-and-cycle-roles-design.md``
(Part B). The Tk editor for these criteria (``RoleCriteriaPanel``) lands in the
follow-up task and will live in THIS module below the core — the
``range_check.py`` / ``market_gap_dialog.py`` house shape (pure core first, Tk
shell after it, Tk imported by the shell only so the core stays import-cheap
and headless-testable).

Load-bearing decisions, each one a place this feature could be silently wrong:

* **Matching has ONE owner: ``fleet_composer.pilot_matches``** (the public alias
  of the fleet composer's rule matcher). This vocabulary is deliberately a
  SUBSET of that matcher's condition types, so a cycle-group "Tag" filter and a
  fleet-template tag rule can never disagree about what a hull is.
* **Fail closed.** ``normalize_criteria`` KEEPS a criterion whose kind is not in
  ``KINDS``, and such a criterion matches nobody: a typo'd include yields an
  EMPTY ring (visible in the dialog's live-match line) rather than silently
  widening to everyone, and a typo'd exclude excludes nobody. That is also why
  ``_matches`` gates on ``KINDS`` BEFORE delegating — the matcher answers True
  to condition types this vocabulary does not offer (``default`` matches every
  pilot, ``character`` matches by name), and a hand-edited config must not be
  able to reach them through a cycle group.
* **Members arrive pre-filtered to CLASSIFIED characters.** The caller drops
  clients with no overlay state or no ``ship_type_id`` (a login screen has no
  role, and an unclassifiable pilot must never be focused by "cycle Subcaps").
  Nothing here re-derives that; a member missing fields simply matches nothing.
* **Ring order is alphabetical, casefolded.** A computed ring has no manual
  order to preserve — that is a Characters-mode feature — so it is sorted for
  determinism instead of inheriting whatever order the client scan produced.
"""
from __future__ import annotations

from types import SimpleNamespace

from fleet_composer import pilot_matches

# The criterion vocabulary — a SUBSET of fleet_composer's condition types.
KINDS = ("ship_type", "ship_class", "doctrine_tag", "capital", "subcap")

# kind -> the label the dialog shows, plus its exact inverse.
KIND_LABELS = {
    "ship_type": "Ship type",
    "ship_class": "Ship class",
    "doctrine_tag": "Tag",
    "capital": "Capitals",
    "subcap": "Subcaps",
}
LABEL_TO_KIND = {label: kind for kind, label in KIND_LABELS.items()}

# Kinds whose criterion means nothing without a value (the dialog blocks OK on
# an empty one); capital/subcap are whole-vocabulary predicates and take none.
VALUE_KINDS = frozenset({"ship_type", "ship_class", "doctrine_tag"})

# One-click starting points, written in the same vocabulary the editor produces
# so a preset stays visible and editable after it is applied. Callers DEEP-COPY
# before handing one to a group — these lists are module state.
PRESETS = {
    "Subcaps": [{"kind": "subcap", "value": "", "exclude": False}],
    "Capitals": [{"kind": "capital", "value": "", "exclude": False}],
}


def normalize_criteria(raw) -> list[dict]:
    """`raw` — anything at all, straight off config — as a list of well-formed
    criterion dicts. Never raises.

    Non-list input, and any entry that is not a dict, is dropped. Every kept
    entry becomes exactly `{"kind": str, "value": str, "exclude": bool}`, with
    missing (or None, or otherwise falsy) `kind`/`value` defaulting to `""` and
    `exclude` to False. An UNKNOWN kind is KEPT on purpose — see the module
    docstring's fail-closed note. Returns fresh dicts: the caller's config
    structures are never aliased, so an edit here can never reach the store."""
    if not isinstance(raw, (list, tuple)):
        return []
    out = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        out.append({
            "kind": str(entry.get("kind") or ""),
            "value": str(entry.get("value") or ""),
            "exclude": bool(entry.get("exclude")),
        })
    return out


def _matches(criterion, member, tag_index):
    """One normalized criterion against one member, via the single matcher."""
    kind = criterion["kind"]
    if kind not in KINDS:
        return False        # fail closed; never reach the matcher's other types
    if kind in VALUE_KINDS and not criterion["value"]:
        # An empty value must never match: pilot_matches would equate "" with an
        # UNRESOLVED hull name (ship_type_name/ship_class default to "" too) —
        # fail closed instead of matching the ghost.
        return False
    condition = SimpleNamespace(type=kind, value=criterion["value"])
    return bool(pilot_matches(condition, member, tag_index))


def role_ring(criteria, members, tag_index) -> list[str]:
    """Ordered char keys for a roles-mode cycle group.

    `criteria` is the group's stored list (normalized here, so callers may pass
    it raw); `members` is an iterable of dicts shaped `{"name",
    "ship_type_name", "ship_class", "ship_type_id", "is_capital"}` — the
    caller's snapshot of the live, CLASSIFIED clients; `tag_index` is
    `{ship_type_id: {tag, ...}}` from the active doctrine (None/empty means no
    doctrine, so tag criteria match nothing).

    A member is in the ring iff it matches at least one INCLUDE criterion — or
    there are none, so an only-excludes list reads as "everyone but…" — and
    matches NO exclude. Names come back sorted casefold-alphabetically and
    deduped; a member with no name is dropped (it cannot be a ring stop).
    Inputs are never mutated."""
    crits = normalize_criteria(criteria)
    includes = [c for c in crits if not c["exclude"]]
    excludes = [c for c in crits if c["exclude"]]
    index = tag_index or {}
    names = []
    for member in members or ():
        name = member.get("name") if isinstance(member, dict) else None
        if not name:
            continue
        if includes and not any(_matches(c, member, index) for c in includes):
            continue
        if any(_matches(c, member, index) for c in excludes):
            continue
        names.append(str(name))
    return list(dict.fromkeys(sorted(names, key=str.casefold)))
