"""Fleet-wide DPS / volley / EHP, rolled up from per-HULL fit simulations.

PURE, stdlib only: no Tk, no network, no ``fc_gui``, and deliberately no
``fit_sim_stats`` import either. Every edge arrives as an injected callable --
``resolve`` (hull id -> ``Fit``), ``simulate`` (``ParsedFit`` -> ``FitStats``)
and ``hull_name`` (hull id -> display name) -- which is what lets the whole
rollup be tested with fakes, and what keeps the dogma table's ~45 ms decode out
of every consumer that only wants the arithmetic.

Four rules shape the module, and each is load-bearing rather than tidy:

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
4. **Only DPS-tagged ships count -- when a doctrine says which those are.**
   (Owner rule, 2026-09-07.) A fleet's damage is what its damage dealers put
   out; folding the logi wing's zero and the tackle's forty into the same
   figure -- and then averaging the fleet's EHP over a pilot count that
   includes them -- describes a fleet nobody is flying. So with a doctrine
   ACTIVE the rollup keeps only hulls whose resolved fit carries the ``DPS``
   tag, and the rest are counted in ``non_dps`` and never simulated at all
   (the cheap direction as well as the honest one). With NO doctrine there is
   no tag to read, so every resolved hull counts, exactly as before.
   ``dps_filter`` records which of the two happened and the tooltip prints it:
   the same ``DPS 41.2k`` means two different things under the two rules, and
   a number that changes meaning with the doctrine dropdown while looking
   identical is the worst of the three options.

``FleetStatsVM`` is frozen and hashable (tuples only, no dicts) because it
rides inside ``info_tiles.FleetCompModel``, whose renderer early-returns on an
unchanged dirty key -- a mutable member there would silently repaint, or worse,
silently NOT repaint.

``fit_sim_links`` is the ONE sibling import: :func:`sim_options` validates the
two config strings against that module's own vocabularies, so a hand-edited
``config.json`` cannot push an unknown tier down into the simulator. It is a
vocabulary read, not an engine one -- nothing here loads the dogma table
(``load`` is injected too, for exactly that reason).
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass

import fit_sim_links

log = logging.getLogger(__name__)

#: Seconds before a FAILED aggregate is attempted again for an UNCHANGED fleet.
#: Without it a single transient failure -- ``dogma_data.load()`` answering
#: False because an AV filter briefly refused the bundle open, the documented
#: file-open class -- would be permanent: the key is latched at spawn, so every
#: later poll would find it unchanged and spawn nothing, forever. The map's sov
#: layer carries the same clock for the same reason.
FLEET_STATS_RETRY_S = 60.0

#: The doctrine tag that marks a member as a damage dealer, LOWER-CASED.
#: Doctrine tags are free text: ``fit_models.DEFAULT_TAGS`` merely seeds the
#: vocabulary and the user extends (and re-cases) it at will, so the match is
#: done on the lower-cased tag rather than on the shipped literal. Deliberately
#: a local constant and not an import of ``fit_models``: the coupling is to the
#: STRING an FC types on a doctrine row, not to a list he is free to edit.
DPS_TAG = "dps"

#: ``FleetStatsVM.dps_filter`` -- which of rule 4's two worlds produced the
#: numbers. ``ALL`` = no doctrine, every resolved hull counted (the pre-2026-09
#: behaviour); ``TAG`` = a doctrine was active and only its ``DPS``-tagged fits
#: were summed. The tooltip prints one line per value; nothing else branches.
DPS_FILTER_ALL = "all"
DPS_FILTER_TAG = "dps-tag"


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
    #: The disciplines the simulator actually APPLIED, sorted -- but only when
    #: every modeled hull resolved the same set; ``()`` when the hulls disagree
    #: (a shield fleet with an armor logi wing under ``auto``) or when nothing
    #: resolved. The tooltip prints these instead of the mode, because
    #: ``links: max/shield`` states what the number was computed under while
    #: ``max/auto`` states only that something chose. A union across DIFFERING
    #: hulls would read as one fleet-wide claim that is true of no hull, so the
    #: mixed case deliberately falls back to naming the mode.
    disciplines_applied: tuple = ()
    #: Pilots EXCLUDED by rule 4's DPS-tag filter: hulls that resolved to a fit
    #: the active doctrine does not tag ``DPS``. Disjoint from ``modeled`` and
    #: from ``unmodeled_hulls`` (those never resolved at all), so
    #: ``modeled + non_dps <= total`` always holds. Always 0 under
    #: ``DPS_FILTER_ALL`` -- with no doctrine nothing is filtered.
    non_dps: int = 0
    #: ``DPS_FILTER_TAG`` or ``DPS_FILTER_ALL``: which rule produced ``dps``.
    #: The tooltip states it because the row's wording cannot -- ``DPS 41.2k``
    #: is the same eight characters either way.
    dps_filter: str = DPS_FILTER_ALL


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


def doctrine_fit_tags(doctrine) -> dict:
    """``{fit_id: frozenset(lower-cased tags)}`` for one doctrine.

    Tags live on the doctrine LINK (``DoctrineMember.tags``), not on the fit --
    the same Ishtar is DPS in one doctrine and a ratting hull in another -- so
    this reads members, not the library. Everything is lower-cased on the way
    in: the vocabulary is user-extensible free text, and an FC who typed
    ``Dps`` on one row and ``DPS`` on the next meant the same thing both times.

    A fit named by SEVERAL members of one doctrine (legal: two entries, two
    orders) gets the UNION of their tags -- if any row calls it DPS, it is a
    DPS fit. A member with no ``fit_id`` is dropped; it can match no fit.
    """
    tags: dict = {}
    for member in (getattr(doctrine, "members", None) or ()):
        fit_id = str(getattr(member, "fit_id", "") or "")
        if not fit_id:
            continue
        raw = getattr(member, "tags", None) or ()
        # A bare string is one tag, not a tag per character -- the ONE junk
        # shape a hand-edited library file plausibly produces.
        if isinstance(raw, str):
            raw = (raw,)
        names = frozenset(
            text for text in (str(tag).strip().lower() for tag in raw) if text)
        tags[fit_id] = tags.get(fit_id, frozenset()) | names
    return tags


def doctrine_dps_fit_ids(doctrine, tag: str = DPS_TAG):
    """The doctrine's ``DPS``-tagged fit ids, or ``None`` when none is active.

    ``None`` and ``frozenset()`` are DIFFERENT answers and the aggregate reads
    them differently: ``None`` means "there is no doctrine, so there is no tag
    to filter on" (count every resolved hull -- rule 4's second half), while an
    empty set means "a doctrine is active and tags nothing DPS" (count
    nothing, and say so). Collapsing the two would make an untagged doctrine
    silently report the whole fleet's damage as if it were the damage dealers'.

    Resolved on the Tk thread and handed to the worker as a plain frozenset,
    for the same reason ``doctrine_fit_ids`` is: the doctrine object's member
    list is live store state the Tk thread may rewrite underneath a worker.
    """
    if doctrine is None:
        return None
    wanted = str(tag or "").strip().lower()
    return frozenset(fit_id for fit_id, tags in doctrine_fit_tags(
        doctrine).items() if wanted in tags)


# ── the Tk thread's inputs, made pure ──────────────────────────────────────

def ship_counts_from_snapshot(cached) -> dict:
    """``{hull_type_id: pilots}`` out of ``_last_specialized_args``.

    The snapshot is ``(members, ship_counts, total)`` when a fleet poll has run
    and ``None`` before the first one -- but it is ALSO briefly ``([], {}, 0)``
    (the honest empty fleet the teardown republishes), and anything at all if a
    future caller re-shapes it. Every one of those answers ``{}``: an empty map
    is what the caller reads as "no fleet", and it is returned as a fresh dict
    the worker owns outright, never the live one the Tk thread rewrites.
    """
    if not isinstance(cached, (tuple, list)) or len(cached) != 3:
        return {}
    counts = cached[1]
    if not isinstance(counts, dict):
        return {}
    return {_as_int(hull, 0): _as_int(count, 0)
            for hull, count in counts.items()}


def sim_options(fittings_cfg) -> tuple:
    """``(tier, disciplines)`` from the ``fittings`` config block, VALIDATED.

    ``config.json`` is a hand-editable file and these two strings are handed
    straight to the simulator, so an unknown value is corrected here rather
    than carried: an unknown tier becomes ``none`` (compute nothing extra --
    the safe direction is fewer claims) and an unknown mode becomes ``auto``.
    Both are recorded in ``FleetStatsVM.tier``/``disciplines`` and printed in
    the tooltip, so the correction is visible rather than silent, and one DEBUG
    line per recompute says which value was rejected.
    """
    block = fittings_cfg if isinstance(fittings_cfg, dict) else {}
    tier = str(block.get("sim_links_tier", fit_sim_links.TIER_NONE)
               or fit_sim_links.TIER_NONE)
    if tier not in fit_sim_links.TIERS:
        log.debug("[hud] unknown sim_links_tier %r -> %s", tier,
                  fit_sim_links.TIER_NONE)
        tier = fit_sim_links.TIER_NONE
    mode = str(block.get("sim_links_disciplines", fit_sim_links.MODE_AUTO)
               or fit_sim_links.MODE_AUTO)
    if mode not in fit_sim_links.DISCIPLINE_MODES:
        log.debug("[hud] unknown sim_links_disciplines %r -> %s", mode,
                  fit_sim_links.MODE_AUTO)
        mode = fit_sim_links.MODE_AUTO
    return tier, mode


def retry_due(failed_at, now, retry_s: float = FLEET_STATS_RETRY_S) -> bool:
    """Is an UNCHANGED fleet's failed aggregate owed another attempt?

    ``failed_at`` is ``None`` whenever the last attempt succeeded or one is in
    flight, and a ``time.monotonic()`` stamp when it failed -- so ``False``
    (the common case) costs one identity test. A stamp from the FUTURE, or one
    that is not a number at all, reads as due: the clock only exists to stop a
    retry storm, and refusing to retry forever is the worse failure.
    """
    if failed_at is None:
        return False
    try:
        failed_at = float(failed_at)
        now = float(now)
    except (TypeError, ValueError):
        return True
    # A NaN stamp (or a NaN "now") is not a usable clock reading, and a
    # comparison against it is False either way -- read the same as an
    # unusable stamp (retry) rather than as "never due" (permanent
    # suppression).
    if math.isnan(failed_at) or math.isnan(now):
        return True
    return (now - failed_at) >= float(retry_s)


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
              tier: str, disciplines: str,
              dps_fit_ids=None) -> FleetStatsVM:
    """Roll `ship_counts` (hull type id -> pilot count) up into one VM.

    ``resolve(hull_type_id) -> Fit | None``,
    ``simulate(parsed_fit) -> FitStats``, ``hull_name(hull_type_id) -> str``:
    all three are injected and all three are allowed to fail. ``simulate`` is
    called at most ONCE per hull type (see the module docstring's rule 1);
    hulls are visited count-descending so the call order is deterministic and
    the heaviest part of the fleet is modeled first.

    ``dps_fit_ids`` is rule 4's switch (build it with
    :func:`doctrine_dps_fit_ids`). ``None`` = no doctrine, so no filter: every
    resolved hull counts. A SET = a doctrine is active, and a hull counts only
    when its resolved fit id is in it. A filtered-out hull is not simulated at
    all -- it costs nothing, and its pilots land in ``non_dps`` rather than in
    ``modeled`` or in ``unmodeled_hulls``: it is an EXCLUSION, not a gap, and
    listing the logi wing under "no fit" would send the FC hunting a fit that
    is right there.
    """
    dps = volley = ehp_sum = 0.0
    modeled = total = non_dps = 0
    partial = False
    unresolved = []
    applied = set()
    filtering = dps_fit_ids is not None
    wanted = frozenset(str(fit_id) for fit_id in (dps_fit_ids or ()))

    for hull, raw_count in sorted(
            ((ship_counts or {}).items()),
            key=lambda kv: (-_as_int(kv[1], 0), _as_int(kv[0], 0))):
        count = _as_int(raw_count, 0)
        if count <= 0:
            continue
        total += count
        fit = _call(resolve, hull, default=None)
        if filtering and fit is not None and \
                str(getattr(fit, "id", "") or "") not in wanted:
            # Resolved, and deliberately not counted. Note the ORDER: an
            # unresolvable hull (fit is None) still falls through to the
            # unmodeled branch below under either rule, because "I have no fit
            # for that hull" is a gap in the number no filter can excuse.
            non_dps += count
            continue
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
        applied.add(tuple(sorted(
            str(name) for name
            in (getattr(stats, "disciplines_applied", ()) or ()))))

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
        partial=partial,
        # ONE set across every modeled hull, or nothing: see the field's own
        # note. `applied` holds one sorted tuple per hull, so "they all agree"
        # is exactly "the set has one member".
        disciplines_applied=(next(iter(applied)) if len(applied) == 1
                             else ()),
        non_dps=non_dps,
        dps_filter=DPS_FILTER_TAG if filtering else DPS_FILTER_ALL)


# ── the worker's whole body ────────────────────────────────────────────────

def compute(ship_counts, fits_by_hull, doctrine_ids, *, tier: str,
            disciplines: str, load, simulate, hull_name, dps_fit_ids=None):
    """One aggregate, or ``None`` when there is no answer. WORKER SIDE.

    This is the fc_gui worker's entire body, kept here so the wiring stays
    wiring: ``load`` (``dogma_data.load``), ``simulate`` and ``hull_name`` are
    injected exactly as everywhere else in this module, which is what keeps the
    engine's ~45 ms decode -- and its import -- out of this file.

    ``None`` means "no row this time", and there are two ways to reach it: a
    table that will not load (missing or corrupt bundle) is bailed on ONCE
    rather than letting every hull raise ``DogmaUnavailable``, and anything
    that escapes the rollup costs the row and nothing else. It is distinct from
    a VM with ``modeled == 0``, which is the honest "the fleet flies nothing I
    have a fit for" -- the caller re-tries a ``None`` on a clock and leaves a
    zero-coverage VM alone. A fleet of pure logi under an active doctrine is
    the same kind of answer: ``modeled 0``, ``non_dps`` = everyone.

    ``dps_fit_ids`` is rule 4's filter, resolved on the Tk thread by
    :func:`doctrine_dps_fit_ids` for the same reason ``doctrine_ids`` is: the
    doctrine object never crosses to a worker.
    """
    try:
        if not _call(load, default=False):
            log.debug("[hud] fleet stats: no dogma table, no aggregate")
            return None
        return aggregate(
            ship_counts,
            # The doctrine OBJECT is deliberately never handed to a worker: its
            # member list is live store state the Tk thread may rewrite, so the
            # ordering was resolved into `doctrine_ids` up there and that plain
            # map is authoritative here.
            lambda hull: fit_for_hull(hull, None, fits_by_hull, doctrine_ids),
            simulate, hull_name, tier=tier, disciplines=disciplines,
            dps_fit_ids=dps_fit_ids)
    except Exception:
        log.debug("[hud] fleet stats aggregate failed", exc_info=True)
        return None
