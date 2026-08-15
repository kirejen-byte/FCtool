"""Pure endpoint/route computation for the star map's wormhole overlay
(EVE-Scout Thera + Turnur connections).

Stdlib-only.  Mirroring the infra_overlay architecture rule, this module NEVER
imports ``wh_route``, fc_gui, map_tab, tkinter, pygame or anything that talks to
the network: connections and routes arrive DUCK-TYPED from the caller (real
``wh_route.WHConnection``/``WHRoute`` dataclasses in production, plain
attribute-carrying fakes in tests).  Every function here is a pure,
deterministic computation over whatever it was handed.

Two products, matching the owner's approved overlay design (2026-08-15):

* :func:`endpoint_markers` -- one aggregated marker per K-space system that has
  a live hole to a hub.  This is the always-on layer: "where can I get in".
* :func:`route_spec` -- the entry->exit spec for the ONE route the Navigation
  tab's "Find WH Shortcut" produced, so the map can draw the full line.

**The hub itself is never part of either payload, by design.**  Thera
(31000005) has NO map coordinates at all -- the bundled ``system_coords`` table
is K-space only (5,485 systems) -- so the hub is literally undrawable.  Turnur
(30002086) IS a real K-space system on the map, but the owner's design hides the
hub for BOTH hubs so the two behave identically: the overlay draws the K-space
ENDPOINTS and the line between them, never a leg to the hub.

DEFENSIVENESS: every value here originates in a third-party API (EVE Scout), so
a missing/None/garbage field DEGRADES the affected field -- it never raises.
Types in the payloads are uniform so the renderer needs no per-field guards:
strings are always ``str`` ("" = unknown) and hour counts always ``int``
(0 = unknown; a genuine 0h hole is indistinguishable from unknown by design --
both mean "do not advertise time remaining").
"""
from __future__ import annotations

import logging
import math

log = logging.getLogger(__name__)

# Ship-size ranking for DISPLAY, smallest to largest.
#
# DELIBERATELY NOT a copy of the ``size_order`` dict in wh_route.find_wh_route,
# which omits "small".  Do NOT "restore lockstep" -- the divergence is the point:
#
# * wh_route's dict is a FILTER FLOOR.  ``ship_size != "any"`` keeps only the
#   connections ranking >= the size the pilot asked for, and nobody filters for
#   "at least frigate-sized", so omitting "small" there is correct.
# * this tuple is a DISPLAY RANKING.  Omitting "small" made a frigate-only
#   system report ``max_size == ""`` -- byte-identical to "size genuinely
#   unknown" -- and the error pointed the DANGEROUS way: an FC sizing a fleet
#   reads "" as "unknown, probably fine", not "nothing bigger than a frigate
#   fits".  EVE Scout's Small is frigates/destroyers only
#   (docs/eve_reference/mechanics.md), so naming the class is the safe report.
#
# Anything outside this tuple ("", None, junk) still does NOT rank: it can never
# win ``max_size`` and, when nothing else ranks, leaves ``max_size`` as "".
SIZE_ORDER = ("small", "medium", "large", "xlarge", "capital")


def _text(value) -> str:
    """A display string for a possibly-missing API field: "" for None/blank."""
    if value is None:
        return ""
    if not isinstance(value, str):
        value = str(value)
    return value.strip()


def _as_int(value) -> int | None:
    """Best-effort int, or None when the value cannot be one.

    Accepts ints, finite floats (truncated) and numeric strings; rejects bools
    (a bool system id / hour count is nonsense, and ``isinstance(True, int)``
    would otherwise let it through), non-finite values and anything unparsable.
    Returns None rather than raising so one malformed EVE Scout record cannot
    take down the overlay.

    TOTALITY IS LOAD-BEARING HERE, not paranoia.  ``fetch_connections`` parses
    EVE Scout with ``resp.json()``, and ``json.loads`` accepts bare ``NaN``,
    ``Infinity`` and ``1e400`` BY DEFAULT -- so non-finite floats really do
    reach this function.  ``int(nan)`` raises ValueError and ``int(inf)``
    raises OverflowError, and both used to escape every caller: ONE such field
    took down the entire overlay.  Hence the ``isfinite`` guards; None then
    flows into the existing ``_int_or_zero``/``_system_id`` degradation."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if math.isfinite(value) else None
    # OverflowError is belt-and-braces: the isfinite guards intercept every
    # overflow reachable today, but a function whose contract is "never raises"
    # must not depend on that reasoning surviving a future edit.
    try:
        return int(str(value).strip())
    except (TypeError, ValueError, OverflowError):
        pass
    try:                       # "3.5" -> 3 (EVE Scout has shipped floats here)
        # float() does NOT raise on overflow -- "1e400" and a 5000-digit string
        # both come back as inf.  On THIS path that's redundant
        # belt-and-braces, not load-bearing: int(parsed) sits INSIDE this try,
        # so dropping the isfinite guard still leaves the except
        # OverflowError to catch it, and dropping OverflowError from the
        # except still leaves the guard returning None before int(parsed)
        # ever runs.  isfinite is truly load-bearing only on the native-float
        # branch above (~line 89), which has no try at all.
        parsed = float(str(value).strip())
        return int(parsed) if math.isfinite(parsed) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _int_or_zero(value) -> int:
    """:func:`_as_int` with the payload's 0-means-unknown sentinel applied."""
    parsed = _as_int(value)
    return 0 if parsed is None else parsed


def _system_id(conn) -> int | None:
    """The K-space system id a connection lands in, or None when unusable.

    Falsy ids (0, "", None) are unusable, not "system 0" -- ``fetch_connections``
    defaults a missing ``in_system_id`` to 0, and an unplaceable connection must
    not become a marker at the origin of the map."""
    parsed = _as_int(getattr(conn, "dest_system_id", None))
    return parsed or None


def _size_rank(value) -> int:
    """Index into SIZE_ORDER, or -1 for anything that does not rank.
    Case-insensitive: the canonical lowercase name is what gets reported."""
    text = _text(value).casefold()
    return SIZE_ORDER.index(text) if text in SIZE_ORDER else -1


def endpoint_markers(connections) -> dict[int, dict]:
    """dest_system_id -> aggregated marker payload for every K-space system with
    a live hole to Thera/Turnur.

    Aggregation is REQUIRED, not incidental: several systems can host holes to
    the same hub, and one system can host several signatures (even one hole to
    each hub at once).  Each surviving system yields exactly these keys:

    * ``"hubs"``   -- sorted tuple of the distinct hub names touching this
      system (usually one; both when it holds a Thera AND a Turnur hole).
      Unnamed hubs are dropped -- "" is not a label a renderer can draw.
    * ``"count"``  -- number of live connections here.  Always equals
      ``len(payload["sigs"])``; duplicates are NOT collapsed, so the badge count
      and the hover list can never disagree.
    * ``"sec"``    -- the destination security class ("hs"/"ls"/"ns"/"c1"...),
      first non-empty in input order; "" when none is known.
    * ``"min_hours"`` -- the SOONEST remaining_hours among this system's
      connections (the operationally urgent number: how long you have before the
      first hole here dies).  Unparsable/missing hours are skipped; 0 when
      nothing ranked.
    * ``"max_size"`` -- the LARGEST max_ship_size (SIZE_ORDER); "" when nothing
      ranked.
    * ``"sigs"``   -- tuple of per-connection dicts
      ``{"sig", "hub_sig", "hub", "size", "hours", "wh_type"}`` sorted by
      signature, then hub, hub signature, size, hours and type, so rendering and
      hover text are byte-stable regardless of the order EVE Scout returned.
      ``size`` here is the connection's OWN raw size, not the aggregated
      ``max_size``.

    Accepts None / any iterable of connections; a None entry inside the list, or
    a connection with no usable ``dest_system_id``, is skipped."""
    grouped: dict[int, list] = {}
    skipped = 0
    for conn in (connections or ()):
        if conn is None:
            skipped += 1
            continue
        system_id = _system_id(conn)
        if system_id is None:
            skipped += 1
            continue
        grouped.setdefault(system_id, []).append(conn)
    if skipped:
        # ONE line for the whole batch -- never one per connection, this runs
        # over the entire EVE Scout list.  Without it a systematic upstream
        # field rename (every in_system_id suddenly 0) presents as "the overlay
        # shows nothing" with no diagnostics at all.
        log.debug("wh_overlay: skipped %d connection(s) with no usable "
                  "dest_system_id", skipped)

    markers: dict[int, dict] = {}
    for system_id, conns in grouped.items():
        hubs = {_text(getattr(c, "hub_name", None)) for c in conns}
        hours = [h for h in (_as_int(getattr(c, "remaining_hours", None))
                             for c in conns) if h is not None]
        ranks = [r for r in (_size_rank(getattr(c, "max_ship_size", None))
                             for c in conns) if r >= 0]
        sigs = [{
            "sig": _text(getattr(c, "dest_signature", None)),
            "hub_sig": _text(getattr(c, "hub_signature", None)),
            "hub": _text(getattr(c, "hub_name", None)),
            "size": _text(getattr(c, "max_ship_size", None)),
            "hours": _int_or_zero(getattr(c, "remaining_hours", None)),
            "wh_type": _text(getattr(c, "wh_type", None)),
        } for c in conns]
        # Sort by KEY, never by the dicts themselves: on a duplicate signature
        # Python would fall through to comparing dicts and raise TypeError.
        # The key spans EVERY field (all plain str/int, so no dict comparison
        # can sneak back in) because byte-stability is promised for ANY input
        # order: with just (sig, hub, hub_sig), two records tied on those three
        # but differing further along fell through to EVE Scout's own order and
        # swapped whenever the feed reordered them.
        sigs.sort(key=lambda s: (s["sig"], s["hub"], s["hub_sig"],
                                 s["size"], s["hours"], s["wh_type"]))
        markers[system_id] = {
            "hubs": tuple(sorted(h for h in hubs if h)),
            "count": len(conns),
            "sec": next((s for s in (_text(getattr(c, "dest_security_class", None))
                                     for c in conns) if s), ""),
            "min_hours": min(hours) if hours else 0,
            "max_size": SIZE_ORDER[max(ranks)] if ranks else "",
            "sigs": tuple(sigs),
        }
    return markers


def route_spec(wh_route, origin_id=None, dest_id=None) -> dict | None:
    """Normalize a ``WHRoute`` into the drawable overlay spec, or None when the
    route is undrawable.

    Returns None -- draw NOTHING -- only for STRUCTURAL undrawability: the
    route is None, either ``entry_connection``/``exit_connection`` is
    missing, or either endpoint's ``dest_system_id`` is falsy/unusable.
    Both connections None means EVE-Scout returned nothing usable, or that no
    entry/exit pairing was routable at all -- it does NOT mean "no shortcut
    beat the gate route".

    DRAWABLE != WORTH DRAWING -- deliberate, do not "fix" it: ``route_spec``
    deliberately does NOT judge whether the wormhole route beats the direct
    gate route.  ``find_wh_route`` (in ``wh_route.py``) picks whichever
    entry/exit pairing is shortest among ITSELF and returns it
    unconditionally -- it never compares that total against
    ``gate_jumps_direct``, so a route that LOSES to going by gate still
    carries two real connections and still produces a full spec here.
    Whether a losing route should be drawn is a presentation decision, and it
    belongs to the caller -- which also owns the text panel's verdict on the
    same route -- not to this module.  Consequently ``jumps_saved`` can
    legitimately be zero or NEGATIVE in an otherwise valid spec; it must
    never be read as a "worth drawing" signal.  A caller that draws every
    spec it receives WILL draw losing routes.

    Otherwise returns exactly::

        {"hub", "entry_id", "exit_id", "origin_id", "dest_id",
         "entry_sig", "exit_sig", "entry_size", "exit_size",
         "entry_hours", "exit_hours", "jumps_saved", "total_jumps"}

    ``origin_id``/``dest_id`` are the caller's already-resolved route endpoints
    (the gate legs' far ends), normalized to int or None -- the overlay draws
    origin->entry and exit->destination gate legs around the WH line.

    SIGNATURE ASYMMETRY -- deliberate, do not "fix" it: ``entry_sig`` is the
    entry connection's ``dest_signature`` (the K-space signature you scan and
    warp to in the ENTRY system), while ``exit_sig`` is the exit connection's
    ``hub_signature`` (the signature you warp to on the HUB side to come back
    out).  Both are the signature the pilot actually needs at the point of the
    route where it is drawn, which is exactly how ``find_wh_route`` labels its
    two wormhole legs.  Taking ``dest_signature`` for both would print a
    signature the pilot never sees while inside Thera/Turnur.

    ``total_jumps`` is int|None: an unknown total STAYS None (never 0), since
    "0 jumps" and "no total computed" are different facts.  ``jumps_saved``
    degrades to 0."""
    if wh_route is None:
        return None
    entry = getattr(wh_route, "entry_connection", None)
    exit_conn = getattr(wh_route, "exit_connection", None)
    if entry is None or exit_conn is None:
        return None
    entry_id = _system_id(entry)
    exit_id = _system_id(exit_conn)
    if entry_id is None or exit_id is None:
        return None

    # The route carries the hub label; fall back to the connections' own
    # hub_name so a partially-built route still draws with a named hub.
    hub = (_text(getattr(wh_route, "hub_name", None))
           or _text(getattr(entry, "hub_name", None))
           or _text(getattr(exit_conn, "hub_name", None)))

    return {
        "hub": hub,
        "entry_id": entry_id,
        "exit_id": exit_id,
        "origin_id": _as_int(origin_id) or None,
        "dest_id": _as_int(dest_id) or None,
        "entry_sig": _text(getattr(entry, "dest_signature", None)),
        "exit_sig": _text(getattr(exit_conn, "hub_signature", None)),  # hub side!
        "entry_size": _text(getattr(entry, "max_ship_size", None)),
        "exit_size": _text(getattr(exit_conn, "max_ship_size", None)),
        "entry_hours": _int_or_zero(getattr(entry, "remaining_hours", None)),
        "exit_hours": _int_or_zero(getattr(exit_conn, "remaining_hours", None)),
        "jumps_saved": _int_or_zero(getattr(wh_route, "jumps_saved", None)),
        "total_jumps": _as_int(getattr(wh_route, "total_jumps_via_wh", None)),
    }
