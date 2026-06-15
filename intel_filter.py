"""Pure, Tk-free criteria matching for the zKillboard fight-alert intel filter.

This is the foundational module behind the config-driven intel filter that
replaces the old hard-coded fight-alert filtering. It contains only pure
functions and constant data: no Tkinter, no network, no file IO, no imports of
other project modules. Everything here is deterministic and safe to call from
any thread.

The shared config schema these helpers operate on (other parts of the app
depend on these exact shapes)::

    config["intel_filter"] = {
        "combine": "AND" | "OR",
        "location": {
            "anywhere": bool,
            "systems": [ {"id": int, "name": str}, ... ],
            "regions": [ {"id": int, "name": str}, ... ],
        },
        "parties": {
            "anyone": bool,
            "alliances":    [ {"id": int, "name": str}, ... ],
            "corporations": [ {"id": int, "name": str}, ... ],
            "coalitions":   [ "<coalition name>", ... ],
        },
    }

    config["coalitions"] = {
        "<name>": {
            "alliances":    [ {"id": int, "name": str}, ... ],
            "corporations": [ {"id": int, "name": str}, ... ],
        },
        ...
    }

Empty-group semantics (important, and applied uniformly):
    A group with no active constraint never silently drops everything. If a
    location/parties section is *not* set to its "match-all" flag yet also
    lists nothing to match against, it is treated as "no constraint" and
    matches everything. This keeps a half-configured filter from hiding all
    alerts. See ``location_matches`` / ``parties_match``.

Scope note: matching here covers only location + parties. The min-pilots and
max-jumps thresholds are intentionally *not* applied by ``matches`` — callers
layer those on separately.
"""

from __future__ import annotations

import copy
from typing import Iterable, Mapping, Optional

__all__ = [
    "expand_parties",
    "location_matches",
    "parties_match",
    "matches",
    "DEFAULT_COALITIONS_SEED",
    "build_default_coalitions",
]


# ──────────────────────────────────────────────────────────────────────────
# Internal helpers
# ──────────────────────────────────────────────────────────────────────────

def _collect_ids(items: object) -> set[int]:
    """Best-effort extraction of integer ``id`` values from a list of dicts.

    Robust to ``items`` being ``None`` or not a list, to non-dict elements,
    and to dicts whose ``id`` is missing or not coercible to ``int``. Booleans
    are rejected (``True``/``False`` are ints in Python but never valid ids).
    Any such element is simply skipped rather than raising.
    """
    out: set[int] = set()
    if not isinstance(items, (list, tuple, set)):
        return out
    for item in items:
        if not isinstance(item, Mapping):
            continue
        raw = item.get("id")
        if isinstance(raw, bool):  # bool is a subclass of int — never an id
            continue
        if isinstance(raw, int):
            out.add(raw)
            continue
        # Tolerate numeric strings / floats that represent whole ids.
        try:
            out.add(int(raw))  # type: ignore[arg-type]
        except (TypeError, ValueError, OverflowError):
            continue
    return out


def _as_int_set(values: object) -> set[int]:
    """Coerce an iterable of involved ids into a ``set[int]``.

    Accepts a list or set (or any iterable) of ids; tolerates ``None`` and
    non-iterable input by returning an empty set. Skips elements that are not
    integers (booleans excluded) and not coercible to int.
    """
    if values is None:
        return set()
    if isinstance(values, (str, bytes)):
        # A bare string is not a meaningful collection of ids here.
        return set()
    try:
        iterator: Iterable = iter(values)  # type: ignore[assignment]
    except TypeError:
        return set()
    out: set[int] = set()
    for v in iterator:
        if isinstance(v, bool):
            continue
        if isinstance(v, int):
            out.add(v)
            continue
        try:
            out.add(int(v))  # type: ignore[arg-type]
        except (TypeError, ValueError, OverflowError):
            continue
    return out


# ──────────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────────

def expand_parties(
    parties: dict,
    coalitions: dict,
) -> tuple[set[int], set[int]]:
    """Expand a ``parties`` selection into concrete (alliance_ids, corp_ids).

    Direct ids come from ``parties["alliances"]`` and ``parties["corporations"]``.
    Additionally, every name in ``parties["coalitions"]`` that exists as a key
    in ``coalitions`` contributes that coalition's alliance and corporation ids.

    Robust to:
      * ``parties`` / ``coalitions`` not being dicts or being ``None``,
      * any of the expected list keys being missing,
      * coalition names that are unknown (silently skipped),
      * item dicts lacking a usable integer ``id`` (silently skipped).

    Returns a 2-tuple of sets ``(alliance_ids, corp_ids)``.
    """
    alliance_ids: set[int] = set()
    corp_ids: set[int] = set()

    if not isinstance(parties, Mapping):
        return alliance_ids, corp_ids

    # Direct selections.
    alliance_ids |= _collect_ids(parties.get("alliances"))
    corp_ids |= _collect_ids(parties.get("corporations"))

    # Coalition membership expansion.
    coalition_names = parties.get("coalitions")
    if isinstance(coalition_names, (list, tuple, set)) and isinstance(
        coalitions, Mapping
    ):
        for name in coalition_names:
            entry = coalitions.get(name) if name is not None else None
            if not isinstance(entry, Mapping):
                continue  # unknown / malformed coalition -> skip
            alliance_ids |= _collect_ids(entry.get("alliances"))
            corp_ids |= _collect_ids(entry.get("corporations"))

    return alliance_ids, corp_ids


def location_matches(
    system_id: Optional[int],
    region_id: Optional[int],
    location: dict,
) -> bool:
    """Return whether an event's system/region satisfies the location filter.

    Rules, in order:
      1. If ``location["anywhere"]`` is truthy -> match (True).
      2. Otherwise, build the set of configured system ids and region ids.
         Match if ``system_id`` is among the systems OR ``region_id`` is among
         the regions.
      3. If ``anywhere`` is falsy AND both lists are empty (no constraint at
         all), return True — a half-configured filter must never silently drop
         everything.

    Never raises on malformed ``location``; treats a non-dict as "no
    constraint" (True).
    """
    if not isinstance(location, Mapping):
        return True

    if location.get("anywhere"):
        return True

    system_ids = _collect_ids(location.get("systems"))
    region_ids = _collect_ids(location.get("regions"))

    if not system_ids and not region_ids:
        # No constraint configured -> do not drop everything.
        return True

    if system_id is not None and not isinstance(system_id, bool) and system_id in system_ids:
        return True
    if region_id is not None and not isinstance(region_id, bool) and region_id in region_ids:
        return True
    return False


def parties_match(
    alliances_involved: object,
    corps_involved: object,
    parties: dict,
    coalitions: dict,
) -> bool:
    """Return whether the involved alliances/corps satisfy the parties filter.

    Rules, in order:
      1. If ``parties["anyone"]`` is truthy -> match (True).
      2. Otherwise expand the selection via :func:`expand_parties` and match if
         the involved alliances intersect the selected alliance ids OR the
         involved corporations intersect the selected corp ids.
      3. If ``anyone`` is falsy AND the expansion yields no ids at all (no
         constraint), return True.

    ``alliances_involved`` / ``corps_involved`` may be a list or a set (or any
    iterable) of ids; ``None`` is treated as empty. Never raises on malformed
    input; a non-dict ``parties`` is treated as "no constraint" (True).
    """
    if not isinstance(parties, Mapping):
        return True

    if parties.get("anyone"):
        return True

    alliance_ids, corp_ids = expand_parties(parties, coalitions)

    if not alliance_ids and not corp_ids:
        # No constraint configured -> do not drop everything.
        return True

    involved_alliances = _as_int_set(alliances_involved)
    involved_corps = _as_int_set(corps_involved)

    if involved_alliances & alliance_ids:
        return True
    if involved_corps & corp_ids:
        return True
    return False


def matches(
    system_id: Optional[int],
    region_id: Optional[int],
    alliances_involved: object,
    corps_involved: object,
    intel_filter: dict,
    coalitions: dict,
) -> bool:
    """Top-level predicate: does an event pass the whole intel filter?

    Combines the location and parties sub-checks per
    ``intel_filter["combine"]`` (defaulting to ``"AND"``)::

        combine == "AND"  ->  location AND parties
        combine == "OR"   ->  location OR  parties

    Any value other than ``"OR"`` (case-sensitive) for ``combine`` is treated
    as ``"AND"``.

    Accepts ``None`` for ids and either a list or a set for the involved
    collections. Never raises on malformed input; a non-dict ``intel_filter``
    degrades to its empty-constraint behaviour (both sub-checks return True, so
    the event passes).

    Note: this deliberately does NOT apply min-pilots or max-jumps thresholds —
    those are layered on by the caller.
    """
    if not isinstance(intel_filter, Mapping):
        intel_filter = {}

    combine = intel_filter.get("combine", "AND")

    location_cfg = intel_filter.get("location", {})
    parties_cfg = intel_filter.get("parties", {})

    loc = location_matches(system_id, region_id, location_cfg)
    par = parties_match(
        alliances_involved or set(),
        corps_involved or set(),
        parties_cfg,
        coalitions,
    )

    if combine == "OR":
        return loc or par
    return loc and par


# ──────────────────────────────────────────────────────────────────────────
# Default coalition seed data
# ──────────────────────────────────────────────────────────────────────────
#
# Snapshot of coalition membership used to seed a fresh config. Values are
# {"alliances": [{"id", "name"}, ...], "corporations": []}. Corporation lists
# are intentionally empty for all seeded coalitions — membership is expressed
# at the alliance level.
#
# Initiative handling:
#   * "Imperium" intentionally EXCLUDES The Initiative. (alliance id
#     1900696668) — The Initiative. is broken out into its own coalition.
#   * "The Initiative." lists ONLY The Initiative. (1900696668). Its frequent
#     partner Triumvirate. is resolved by id at migration time inside the GUI
#     (the seed cannot know that id), so it is added via
#     ``build_default_coalitions(triumvirate_id=...)`` rather than baked in
#     here.

DEFAULT_COALITIONS_SEED: dict = {
    "Imperium": {
        "alliances": [
            {"id": 1354830081, "name": "Goonswarm Federation"},
            {"id": 99003214, "name": "Brave Collective"},
            {"id": 99009163, "name": "Dracarys."},
            {"id": 131511956, "name": "Tactical Narcotics Team"},
            {"id": 99003995, "name": "Invidia Gloriae Comes"},
            {"id": 99001969, "name": "SONS of BANE"},
            {"id": 99011223, "name": "Sigma Grindset"},
            {"id": 99011162, "name": "Shadow Ultimatum"},
            {"id": 99009331, "name": "Scumlords"},
            {"id": 99012042, "name": "Fanatic Legion."},
            {"id": 99010877, "name": "Out of the Blue."},
        ],
        "corporations": [],
    },
    "Winter Coalition": {
        "alliances": [
            {"id": 99003581, "name": "Fraternity."},
            {"id": 386292982, "name": "Pandemic Legion"},
            {"id": 1727758877, "name": "Northern Coalition."},
            {"id": 99013537, "name": "Insidious."},
            {"id": 99002685, "name": "Synergy of Steel"},
            {"id": 498125261, "name": "TEST Alliance Please Ignore"},
            {"id": 99005393, "name": "Blades of Grass"},
            {"id": 99007203, "name": "Siberian Squads"},
            {"id": 99014657, "name": "Ranger Regiment"},
            {"id": 1411711376, "name": "Legion of xXDEATHXx"},
            {"id": 1042504553, "name": "Solyaris Chtonium"},
        ],
        "corporations": [],
    },
    "The Initiative.": {
        "alliances": [
            {"id": 1900696668, "name": "The Initiative."},
        ],
        "corporations": [],
    },
}


def build_default_coalitions(
    triumvirate_id: Optional[int] = None,
    triumvirate_name: str = "Triumvirate.",
) -> dict:
    """Return a deep copy of :data:`DEFAULT_COALITIONS_SEED`.

    A deep copy is returned so callers can freely mutate the result without
    touching the module-level seed.

    If ``triumvirate_id`` is provided, an entry
    ``{"id": triumvirate_id, "name": triumvirate_name}`` is appended to the
    ``"The Initiative."`` coalition's alliance list. (Triumvirate.'s id is not
    known to this module; the GUI resolves it at migration time and passes it
    in.) A ``None`` id leaves the seed membership unchanged.
    """
    coalitions = copy.deepcopy(DEFAULT_COALITIONS_SEED)

    if triumvirate_id is not None and not isinstance(triumvirate_id, bool):
        coalitions["The Initiative."]["alliances"].append(
            {"id": triumvirate_id, "name": triumvirate_name}
        )

    return coalitions
