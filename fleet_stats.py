"""Fleet-wide DPS / volley / EHP, rolled up from per-HULL fit simulations.

PURE, stdlib only: no Tk, no network, no ``fc_gui``, and deliberately no
``fit_sim_stats`` import either. Every edge arrives as an injected callable --
``resolve`` (hull id -> ``Fit``), ``simulate`` (``ParsedFit`` -> ``FitStats``)
and ``hull_name`` (hull id -> display name) -- which is what lets the whole
rollup be tested with fakes, and what keeps the dogma table's ~45 ms decode out
of every consumer that only wants the arithmetic.

Three rules shape the module, and each is load-bearing rather than tidy:

1. **Per HULL, never per member.** A 200-pilot fleet flying 8 hulls costs 8
   simulations, not 200 -- and ``fit_sim_stats.simulate``'s own LRU makes the
   repeat runs free. The count is a MULTIPLIER, applied after the sim.
2. **Honest partials.** A hull with no resolvable fit is not silently dropped:
   it lands in ``unmodeled_hulls`` and is missing from ``modeled``, so the tile
   can print ``(31/35)`` and the FC can see how much of his fleet the number
   actually covers. A number that quietly describes 60 % of the fleet while
   looking like all of it is worse than no number.
3. **Never raise.** A resolver that throws, a fit whose ``parsed`` is missing,
   a simulate that hits an unmodellable hull -- each costs that hull's
   contribution and nothing else. This runs on a worker feeding a 1 Hz HUD
   tile; a raise there is a dead tile, not an error message.

``FleetStatsVM`` is frozen and hashable (tuples only, no dicts) because it
rides inside ``info_tiles.FleetCompModel``, whose renderer early-returns on an
unchanged dirty key -- a mutable member there would silently repaint, or worse,
silently NOT repaint.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FleetStatsVM:
    """One fleet's aggregate, as the HUD tile shows it.

    ``dps``/``volley`` are sums over the MODELED hulls only; ``ehp_avg`` is the
    modeled-weighted mean of ``FitStats.ehp_total`` (a per-pilot average, so a
    fleet of 30 cruisers and 1 titan does not read as a titan fleet).
    ``modeled``/``total`` are PILOT counts, not hull-type counts.

    ``tier``/``disciplines`` are recorded verbatim so the tooltip can state the
    assumption the numbers were computed under, and ``partial`` is True when
    any modeled fit carried something the engine could not model -- the tile
    prefixes ``~`` rather than pretending to precision it does not have.

    Frozen and hashable by contract: see the module docstring.
    """

    dps: float
    volley: float
    ehp_avg: float
    modeled: int
    total: int
    #: Hull names with no resolvable (or no simulatable) fit, count-descending.
    unmodeled_hulls: tuple
    tier: str
    disciplines: str
    partial: bool


# ── small guards (nothing in this module may raise on junk) ─────────────────

def _as_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    if result != result or result in (float("inf"), float("-inf")):
        # NaN/inf would poison every later sum and print as "nan" on the tile.
        return default
    return result


def _call(fn, *args, default=None):
    """Call an injected seam; absent, non-callable or raising all answer
    `default`. The house guard -- an aggregate that dies because one hull's
    name lookup threw would take the whole tile with it."""
    if not callable(fn):
        return default
    try:
        return fn(*args)
    except Exception:
        return default


def _hull_label(hull_type_id, hull_name) -> str:
    """A printable name for one hull. An unresolvable id reads as
    ``Type 12345`` -- honest about the gap, and still distinct from its
    neighbours in the tooltip's list."""
    name = _call(hull_name, hull_type_id, default=None)
    text = str(name).strip() if name else ""
    return text or f"Type {_as_int(hull_type_id, 0)}"


# ── fit resolution ─────────────────────────────────────────────────────────

def _ordered_doctrine_ids(hull_type_id, doctrine, by_id,
                          doctrine_fit_ids) -> list:
    """The doctrine's fit ids for one hull, best FIRST.

    A precomputed ``doctrine_fit_ids`` mapping is AUTHORITATIVE: a hull absent
    from it means "the doctrine names no fit for this hull", not "ask the
    doctrine object". ``None`` (nothing precomputed) falls back to walking the
    doctrine's members, ordered by ``DoctrineMember.order`` with declaration
    order breaking ties -- so two members sharing an order still resolve the
    same way on every call, which the aggregator's key stability depends on.
    """
    if isinstance(doctrine_fit_ids, dict):
        return [str(fit_id) for fit_id
                in (doctrine_fit_ids.get(hull_type_id) or ())]
    rows = []
    for index, member in enumerate(getattr(doctrine, "members", None) or ()):
        fit_id = str(getattr(member, "fit_id", "") or "")
        if fit_id and fit_id in by_id:
            rows.append((_as_int(getattr(member, "order", 0), 0),
                         index, fit_id))
    rows.sort()
    return [fit_id for _order, _index, fit_id in rows]


def fit_for_hull(hull_type_id, doctrine, fits_by_hull, doctrine_fit_ids):
    """The fit this fleet's <hull> is assumed to be flying, or None.

    The ladder, in order (spec section 5.6):

    1. A DOCTRINE fit for the hull -- if several, the member with the lowest
       ``DoctrineMember.order``. The doctrine is the FC's own statement of what
       the fleet is flying, so it outranks the library every time.
    2. Otherwise the library's fit for the hull, and only when there is exactly
       ONE. Two library fits for the same hull is a genuine ambiguity (a shield
       and an armor Loki are different ships for every number here), and
       guessing would put a confident wrong figure on the HUD.
    3. Otherwise None -- the hull is counted as unmodeled and named in the
       tooltip.
    """
    candidates = list((fits_by_hull or {}).get(hull_type_id) or ())
    by_id = {}
    for fit in candidates:
        fit_id = str(getattr(fit, "id", "") or "")
        if fit_id and fit_id not in by_id:
            by_id[fit_id] = fit
    for fit_id in _ordered_doctrine_ids(hull_type_id, doctrine, by_id,
                                        doctrine_fit_ids):
        fit = by_id.get(fit_id)
        if fit is not None:
            return fit
    if len(candidates) == 1:
        return candidates[0]
    return None


def index_fits(fits) -> dict:
    """``[Fit] -> {hull_type_id: [Fit]}``, in the library's own order.

    Built once per recompute on the Tk thread and handed to the worker as a
    plain dict: the worker must never reach back into ``FittingsStore`` (an
    internally locked object the Tk thread also writes). A fit with no usable
    hull id is dropped rather than bucketed under None -- it can never match a
    fleet member anyway."""
    index: dict = {}
    for fit in (fits or ()):
        hull = getattr(fit, "hull_type_id", None)
        if hull is None:
            continue
        index.setdefault(_as_int(hull, 0), []).append(fit)
    return index


def doctrine_fit_ids(doctrine, fits_by_hull) -> dict:
    """``{hull_type_id: [fit_id, ...]}`` for one doctrine, best fit FIRST.

    The doctrine's ordering resolved ONCE, on the Tk thread, out of live store
    state -- ``fit_for_hull`` then reads a plain map instead of walking a
    member list the Tk thread may be rewriting underneath it. Ordered by
    ``DoctrineMember.order``, declaration order breaking ties. ``None``
    (no active doctrine) answers ``{}``: an empty map is still AUTHORITATIVE,
    which is exactly right -- there is no doctrine fit for any hull."""
    if doctrine is None:
        return {}
    hull_of = {}
    for hull, fits in (fits_by_hull or {}).items():
        for fit in fits:
            hull_of[getattr(fit, "id", None)] = hull
    rows = []
    for index, member in enumerate(getattr(doctrine, "members", None) or ()):
        fit_id = getattr(member, "fit_id", None)
        hull = hull_of.get(fit_id)
        if hull is not None:
            rows.append((hull, _as_int(getattr(member, "order", 0), 0), index,
                         str(fit_id)))
    ordered: dict = {}
    for hull, _order, _index, fit_id in sorted(rows, key=lambda r: r[:3]):
        ordered.setdefault(hull, []).append(fit_id)
    return ordered


# ── recompute key ──────────────────────────────────────────────────────────

def fleet_key(ship_counts, doctrine_id, revision, tier, disciplines) -> tuple:
    """The hashable identity of one aggregate: recompute only when it moves.

    ``ship_counts`` is SORTED into the key, so the same fleet arriving in a
    different dict order is the same key -- the whole point, since the fleet
    poll rebuilds that dict every ~30 s from an ESI response whose member order
    is not stable. Zero and negative counts are dropped rather than recorded:
    they contribute nothing to the answer, so they must not force a recompute.

    ``revision`` is ``FittingsStore.revision()`` -- editing a doctrine fit has
    to invalidate the aggregate even when the fleet did not change.
    """
    counts = tuple(sorted(
        (_as_int(hull, 0), _as_int(count, 0))
        for hull, count in ((ship_counts or {}).items())
        if _as_int(count, 0) > 0))
    return (counts, str(doctrine_id or ""), _as_int(revision, 0),
            str(tier or ""), str(disciplines or ""))


# ── the rollup ─────────────────────────────────────────────────────────────

def aggregate(ship_counts, resolve, simulate, hull_name, *,
              tier: str, disciplines: str) -> FleetStatsVM:
    """Roll `ship_counts` (hull type id -> pilot count) up into one VM.

    ``resolve(hull_type_id) -> Fit | None``,
    ``simulate(parsed_fit) -> FitStats``, ``hull_name(hull_type_id) -> str``:
    all three are injected and all three are allowed to fail. ``simulate`` is
    called at most ONCE per hull type (see the module docstring's rule 1);
    hulls are visited count-descending so the call order is deterministic and
    the heaviest part of the fleet is modeled first.
    """
    dps = volley = ehp_sum = 0.0
    modeled = total = 0
    partial = False
    unresolved = []

    for hull, raw_count in sorted(
            ((ship_counts or {}).items()),
            key=lambda kv: (-_as_int(kv[1], 0), _as_int(kv[0], 0))):
        count = _as_int(raw_count, 0)
        if count <= 0:
            continue
        total += count
        fit = _call(resolve, hull, default=None)
        parsed = getattr(fit, "parsed", None) if fit is not None else None
        stats = _call(simulate, parsed, default=None) if parsed is not None \
            else None
        if stats is None:
            unresolved.append((count, _hull_label(hull, hull_name)))
            continue
        modeled += count
        dps += _as_float(getattr(stats, "dps_total", 0.0)) * count
        volley += _as_float(getattr(stats, "volley", 0.0)) * count
        ehp_sum += _as_float(getattr(stats, "ehp_total", 0.0)) * count
        partial = partial or bool(getattr(stats, "partial", False))

    return FleetStatsVM(
        dps=dps, volley=volley,
        # Weighted by PILOTS, over the modeled pilots only: dividing by `total`
        # would quietly report a fleet-average EHP that includes ships whose
        # EHP was never computed, i.e. zero.
        ehp_avg=(ehp_sum / modeled) if modeled else 0.0,
        modeled=modeled, total=total,
        unmodeled_hulls=tuple(
            name for _count, name
            in sorted(unresolved, key=lambda row: (-row[0], row[1]))),
        tier=str(tier or ""), disciplines=str(disciplines or ""),
        partial=partial)
