"""Pure overlay-layer computation for the star map (spec §5.2).

All game-data lookups are injectable for tests; defaults bind lazily to
system_coords / jump_range. Range/threat use TRUE 3D LY distances via
system_coords.systems_within_range — never 2D map distance (spec: color
systems, never draw a circle).
"""
from __future__ import annotations

import colorsys
import math
from collections import deque
from dataclasses import dataclass, field


def _default_within(origin_id: int, range_ly: float, legal_only: bool = True):
    import system_coords
    return system_coords.systems_within_range(origin_id, range_ly, legal_only=legal_only)


def _default_legal(sid: int) -> bool:
    import system_coords
    return system_coords.is_legal_jump_destination(sid)


def _default_resolve(name: str):
    import system_coords
    return system_coords.resolve_name(name)


def _default_ship_table() -> dict:
    from jump_range import JumpRangeChecker
    return JumpRangeChecker.SHIP_RANGES


@dataclass(frozen=True)
class RangeOverlay:
    origin_id: int
    range_ly: float
    ship_label: str
    in_range: frozenset[int]
    illegal: frozenset[int]
    distances: dict[int, float] = field(default_factory=dict)

    def bright_set(self) -> frozenset[int]:
        return self.in_range | {self.origin_id}


def compute_range(origin_id: int, range_ly: float, ship_label: str = "",
                  within_fn=None, legal_fn=None) -> RangeOverlay:
    within = within_fn or _default_within
    legal = legal_fn or _default_legal
    in_sphere = within(origin_id, range_ly, legal_only=False)
    in_range, illegal, dists = set(), set(), {}
    for sid, ly in in_sphere:
        if sid == origin_id:
            continue
        dists[sid] = ly
        (in_range if legal(sid) else illegal).add(sid)
    return RangeOverlay(origin_id, range_ly, ship_label,
                        frozenset(in_range), frozenset(illegal), dists)


def compute_threat(staging_ids, range_ly: float, within_fn=None) -> frozenset[int]:
    """Union of systems inside `range_ly` of ANY hostile staging (staging incl.)."""
    within = within_fn or _default_within
    out: set[int] = set()
    for sid in staging_ids:
        out.add(sid)
        for other, _ly in within(sid, range_ly, legal_only=True):
            out.add(other)
    return frozenset(out)


def fleet_counts(members) -> dict[int, int]:
    counts: dict[int, int] = {}
    for m in members or ():
        sid = m.get("solar_system_id")
        if isinstance(sid, int):
            counts[sid] = counts.get(sid, 0) + 1
    return counts


def resolve_staging(names, resolve_fn=None) -> dict[int, str]:
    resolve = resolve_fn or _default_resolve
    out: dict[int, str] = {}
    for name in names or ():
        sid = resolve(name)
        if sid is not None:
            out[sid] = name
    return out


def resolve_bridges(entries, resolve_fn=None) -> tuple[tuple[int, int], ...]:
    """Resolve config ``ansiblex_connections`` name pairs to a deterministic,
    deduped tuple of unordered ``(id_a, id_b)`` system-id pairs for the map's
    Ansiblex bridge layer.

    Mirrors the canonical parse the jump-range BFS consumes
    (``fc_gui._resolve_ansiblex_sync``): each entry is a 2-element
    ``[name_a, name_b]``; each name resolves via ``resolve_fn`` (default
    ``system_coords.resolve_name`` -- pure/local, NO ESI, so it is safe to call
    on the Tk thread). Unresolvable endpoints, self-pairs, malformed entries,
    and duplicate unordered pairs are dropped. The result is sorted and returned
    as a hashable tuple so it can travel in the render request dict and
    participate in the duplicate-render signature (``map_tab._request_sig``)."""
    resolve = resolve_fn or _default_resolve
    seen: set[tuple[int, int]] = set()
    out: list[tuple[int, int]] = []
    for entry in entries or ():
        try:
            if len(entry) != 2:
                continue
            name_a, name_b = entry[0], entry[1]
        except (TypeError, KeyError, IndexError):
            continue
        ida = resolve(name_a)
        idb = resolve(name_b)
        if ida is None or idb is None or ida == idb:
            continue
        pair = (ida, idb) if ida < idb else (idb, ida)
        if pair in seen:
            continue
        seen.add(pair)
        out.append(pair)
    out.sort()
    return tuple(out)


def classify_route_segments(path, bridges):
    """Per-segment classification of a travel route for the map's route overlay
    (Task 35). ``path`` is an ordered sequence of system ids (as returned by the
    Ansiblex-aware stargate BFS ``jump_range.get_stargate_route`` -- origin first,
    destination last). ``bridges`` is an iterable of unordered ``(id_a, id_b)``
    system-id pairs (``resolve_bridges`` output). Returns a list of
    ``(from_id, to_id, kind)`` tuples, one per consecutive hop
    (``len == len(path) - 1``); ``kind`` is ``"bridge"`` when the hop's unordered
    endpoint pair is an Ansiblex pair, else ``"gate"``.

    Mirrors ``fc_gui._find_ansiblex_in_route``'s pair-membership heuristic: the
    BFS adds each bridge as an extra graph edge, so a consecutive bridge pair in
    the resolved path means the route takes that bridge. Pure / headless-testable
    -- no game-data lookups; malformed bridge entries are skipped."""
    bset: set[tuple[int, int]] = set()
    for pr in bridges or ():
        try:
            a, b = pr
        except (TypeError, ValueError):
            continue
        bset.add((a, b))
        bset.add((b, a))
    seq = list(path or ())
    out: list[tuple[int, int, str]] = []
    for i in range(len(seq) - 1):
        a, b = seq[i], seq[i + 1]
        out.append((a, b, "bridge" if (a, b) in bset else "gate"))
    return out


# Owner decision (spec §2.5): 5 grouped classes; labels carry the live LY value
# read from SHIP_RANGES at runtime so the Rorqual/Lancer fix chip auto-propagates.
_GROUPS = [
    ("Titan / Super", "Titan"),
    ("Dread / Carrier / FAX", "Dreadnought"),
    ("Command Carrier", "Command Carrier"),
    ("Black Ops / Lancer", "Black Ops"),
    ("JF / Rorqual", "Jump Freighter"),
]


def range_options(ship_table: dict | None = None) -> list[tuple[str, float]]:
    table = ship_table if ship_table is not None else _default_ship_table()
    opts = []
    for label, key in _GROUPS:
        ly = float(table.get(key, 6.0))
        opts.append((f"{label} ({ly:.1f} ly)", ly))   # .1f matches test labels exactly
    return opts


def threat_options(ship_table: dict | None = None) -> list[tuple[str, float]]:
    """Titan Bridge first (owner default): bridge range = titan jump range."""
    table = ship_table if ship_table is not None else _default_ship_table()
    tb = float(table.get("Titan", 6.0))
    return [(f"Titan Bridge ({tb:.1f} ly)", tb)] + range_options(table)


def ly_for_ship(ship_name: str, ship_table: dict | None = None) -> float:
    table = ship_table if ship_table is not None else _default_ship_table()
    if ship_name == "Titan Bridge":
        return float(table.get("Titan", 6.0))
    return float(table.get(ship_name, table.get("Titan", 6.0)))


# --- kill-heat layer (Task 30) ----------------------------------------------
# Tunable model constants. The LIVE zkill component decays with a 15-minute
# half-life; a single killmail-weight decays to 1/2 in 15 min, 1/4 in 30, so a
# fight stays lit for ~an hour then fades. Capitals are remembered ~30 min for
# the marker overlay (independent of heat magnitude). The per-system cap fixes
# the 0..1 scale to an ABSOLUTE amount of decayed activity: each system's heat is
# min(summed_decayed_weight, cap) / cap, so one hyperactive system can never wash
# out the scale for another (systems are independent), and the intensity reflects
# real activity rather than a floating peak that jumps as the busiest system
# decays. Ambient (hourly ESI ship+pod kills) is scaled into a LOW band so the
# live zkill component always dominates a system that has both.
_HEAT_HALFLIFE_S = 15.0 * 60.0                 # live zkill decay half-life
_HEAT_LAMBDA = math.log(2.0) / _HEAT_HALFLIFE_S  # exp(-lambda * dt) decay rate
_CAP_MEMORY_S = 30.0 * 60.0                    # capital-marker memory window
_HEAT_EVENTS_MAX = 500                         # ring-buffer bound (events)
_PER_SYSTEM_CAP = 10.0                         # decayed weight that reads as 1.0
_AMBIENT_BAND = 0.35                           # ambient heat ceiling (live dominates)
_AMBIENT_FULL = 40.0                           # ship+pod kills that fill the band


class KillHeat:
    """Time-decayed per-system kill heat for the star map's kill-heat layer
    (Task 30). Two components, both surfaced as 0..1 intensities:

      * LIVE zkill events (``add_kill``) land in a bounded ring of
        ``(system_id, count, ts, capital)`` tuples. ``heat_at(now)`` decays each
        event exponentially (15-min half-life), sums per system, and maps the sum
        through a FIXED per-system cap (``min(sum, cap) / cap``) so the intensity
        is an absolute measure of activity and no single system can wash out
        another's scale.
      * AMBIENT hourly ESI kills (``merge_ambient``) are scaled into a LOW
        ``0.._AMBIENT_BAND`` band and combined with the live heat by MAX, so a
        real live fight (heat -> 1.0) always dominates the ambient tint.

    Capitals are tracked separately (``capital_systems``) with ~30-min memory for
    the double-ring marker overlay. Pure / headless-testable: the clock is passed
    in as ``now`` (wall-clock epoch seconds from the MapTab callers), never read
    internally, so tests drive decay deterministically."""

    def __init__(self, maxlen: int = _HEAT_EVENTS_MAX) -> None:
        # (system_id, count, ts, capital). deque(maxlen) evicts oldest -> O(1)
        # bounded memory regardless of kill volume (the ring-bound invariant).
        self._events: deque = deque(maxlen=maxlen)

    def add_kill(self, system_id, count, ts: float, capital: bool = False) -> None:
        """Record one zkill engagement alert. ``count`` is the engagement's
        killmail count (KillAlert.kill_count); coerced to >= 1 so a malformed 0
        still registers a kill. A bad/None system_id is ignored."""
        if system_id is None:
            return
        try:
            sid = int(system_id)
            cnt = max(1, int(count or 1))
        except (TypeError, ValueError):
            return
        self._events.append((sid, cnt, float(ts), bool(capital)))

    def _decayed(self, now: float) -> dict[int, float]:
        """Per-system summed decayed weight (uncapped, unnormalized)."""
        out: dict[int, float] = {}
        lam = _HEAT_LAMBDA
        for sid, count, ts, _cap in self._events:
            dt = now - ts
            if dt < 0.0:
                dt = 0.0                       # clock skew -> treat as "just now"
            out[sid] = out.get(sid, 0.0) + count * math.exp(-lam * dt)
        return out

    def heat_at(self, now: float) -> dict[int, float]:
        """Normalized 0..1 LIVE heat per system at ``now``. Each system's summed
        decayed weight is mapped through the fixed per-system cap (``min(sum, cap)
        / cap``), so values lie in [0, 1], the busiest fights approach 1.0, and no
        system's scale depends on another's. Empty ring -> ``{}``."""
        summed = self._decayed(now)
        return {sid: min(v, _PER_SYSTEM_CAP) / _PER_SYSTEM_CAP
                for sid, v in summed.items()}

    def merge_ambient(self, esi_kills, now: float) -> dict[int, float]:
        """Combine live heat with hourly ESI ambient kills into one 0..1 dict.
        ``esi_kills`` is ``{system_id: ship_kills + pod_kills}`` (see
        ``parse_system_kills``). Ambient counts are scaled into the LOW
        ``0.._AMBIENT_BAND`` band (``min(1, kills / full) * band``) and combined
        with the live heat by MAX -- so an ambient-only system reads at most
        ``_AMBIENT_BAND`` and a live fight always outshines the ambient tint. The
        result stays in [0, 1] with no clamp needed."""
        merged = self.heat_at(now)
        for sid, kills in (esi_kills or {}).items():
            try:
                k = int(kills)
            except (TypeError, ValueError):
                continue
            if k <= 0:
                continue
            amb = min(1.0, k / _AMBIENT_FULL) * _AMBIENT_BAND
            cur = merged.get(sid, 0.0)
            if amb > cur:
                merged[sid] = amb
        return merged

    def capital_systems(self, now: float) -> frozenset[int]:
        """Systems with a capital kill within the ~30-min memory window (for the
        map's double-ring capital-kill marker overlay). Independent of heat
        magnitude -- a capital kill marks its system for ``_CAP_MEMORY_S`` even
        after its heat has decayed away."""
        horizon = now - _CAP_MEMORY_S
        return frozenset(sid for sid, _c, ts, cap in self._events
                         if cap and ts >= horizon)


def canonical_heat(heat: dict, ndigits: int = 2) -> tuple:
    """Canonical hashable signature of a heat dict for the render request tuple
    (Task 30): sorted ``((system_id, round(intensity, ndigits)), ...)`` with
    zero/negligible entries dropped. Rounding to 2dp keeps the value STABLE across
    the tiny per-tick decay drift, so the duplicate-render suppression signature
    (``map_tab._request_sig``) does not change on every 16 ms tick -- a fresh
    crisp is requested only when the rounded heat actually moves. Pure/testable."""
    out = []
    for sid, h in (heat or {}).items():
        r = round(float(h), ndigits)
        if r <= 0.0:
            continue
        out.append((int(sid), r))
    out.sort()
    return tuple(out)


def parse_system_kills(rows) -> dict[int, int]:
    """Parse the ESI ``/universe/system_kills/`` payload into ``{system_id:
    ship_kills + pod_kills}`` for the kill-heat ambient band (Task 30).
    ``npc_kills`` are ratting, not PvP, so they are EXCLUDED. Malformed rows
    (missing ``system_id`` or a non-dict entry) are skipped. Pure/testable."""
    out: dict[int, int] = {}
    for r in rows or ():
        try:
            sid = r.get("system_id")
        except AttributeError:
            continue                           # non-dict row -> skip
        if sid is None:
            continue
        ship = r.get("ship_kills", 0) or 0
        pod = r.get("pod_kills", 0) or 0
        try:
            out[int(sid)] = int(ship) + int(pod)
        except (TypeError, ValueError):
            continue
    return out


# --- intel pulse layer (Task 31) --------------------------------------------
# Systems named in tracked intel channels pulse on the map and fade over a
# 5-minute window. Decay is LINEAR (not exponential like KillHeat): the intensity
# hits an EXACT zero at PULSE_DECAY_S and reads as exactly "half" at the midpoint
# -- a hard, test-pinnable cutoff. An exponential curve only asymptotes toward
# zero, so it would never cull; the linear ramp gives a clean "gone after 5 min".
# The live set is bounded (PULSE_MAX) so pathological intel spam can never grow it
# without limit -- realistic intel traffic never approaches the bound.
PULSE_DECAY_S = 300.0        # a mention is fully faded 5 min after it lands
PULSE_MAX = 200              # eviction bound (oldest mention dropped beyond this)


class IntelPulses:
    """Time-decayed set of intel-mentioned systems for the star map's pulse layer
    (Task 31). Pure / clock-injected -- mirrors KillHeat: every method takes ``now``
    (wall-clock epoch seconds from the MapTab callers) and never reads a clock
    internally, so tests drive the decay deterministically.

      * ``note(system_id, now)`` records or REFRESHES a mention; a re-mention resets
        that system's decay to full (intensity 1.0) by overwriting its timestamp.
      * ``active(now)`` -> ``{system_id: intensity}`` where intensity fades LINEARLY
        1.0 -> 0 over ``PULSE_DECAY_S`` (0.5 at the midpoint, exactly 0 at/after the
        window). Expired entries are dropped from the internal store as a side
        effect (the 'cull'), so memory stays bounded by live activity.
      * ``has_any()`` is an O(1) 'anything to draw?' gate for the MapTab hot path.

    The most-recently-mentioned set is bounded to ``maxlen`` (oldest mention evicted
    first) so intel spam can't grow the store without limit."""

    def __init__(self, maxlen: int = PULSE_MAX) -> None:
        self._maxlen = int(maxlen)
        # system_id -> last-mention wall-clock ts. A plain dict: Python preserves
        # insertion order, and note() re-inserts on refresh, so iteration order is
        # LRU (oldest-first) and eviction simply drops the front entry.
        self._last: dict[int, float] = {}

    def note(self, system_id, now: float) -> None:
        """Record/refresh a mention of ``system_id`` at ``now``. A re-mention resets
        decay to full (the timestamp is overwritten). A bad/None id is ignored.
        Evicts the oldest mention when the set would exceed ``maxlen``."""
        if system_id is None or isinstance(system_id, bool):
            return
        try:
            sid = int(system_id)
        except (TypeError, ValueError):
            return
        self._last.pop(sid, None)            # move-to-end on refresh (LRU order)
        self._last[sid] = float(now)
        while len(self._last) > self._maxlen:
            # drop the oldest (front) mention -- bounded memory under intel spam.
            self._last.pop(next(iter(self._last)), None)

    def has_any(self) -> bool:
        """True iff any mention is stored. Cheap (dict truthiness) -- the MapTab
        empty-fast-path checks this every tick before doing any pulse work. May
        briefly report True for an expired-but-uncalled entry; the next ``active``
        call culls it."""
        return bool(self._last)

    def active(self, now: float) -> dict[int, float]:
        """``{system_id: intensity}`` for every un-expired mention at ``now``, with
        intensity ``= 1 - dt / PULSE_DECAY_S`` in (0, 1]. Expired mentions (dt >=
        PULSE_DECAY_S) are removed from the store as a side effect (the cull, which
        keeps memory bounded by live activity). Clock skew (dt < 0) is treated as
        'just now' -> intensity 1.0."""
        out: dict[int, float] = {}
        expired: list[int] = []
        for sid, ts in self._last.items():
            dt = now - ts
            if dt < 0.0:
                dt = 0.0                     # clock skew -> just now
            if dt >= PULSE_DECAY_S:
                expired.append(sid)
                continue
            out[sid] = 1.0 - dt / PULSE_DECAY_S
        for sid in expired:
            self._last.pop(sid, None)
        return out


# --- sovereignty tint layer (Task 33) ---------------------------------------
# ESI /sovereignty/map/ (public, cached long) gives each system's sovereign
# alliance. We tint each sov'd system with a dim, per-alliance hashed color so
# same-alliance neighbors merge into a soft regional wash BEHIND the glowing
# nodes. OFF by default (owner's call on palette noise) -- these helpers are
# pure/testable and never touch the network (the fetch lives in MapTab).
#
# The color hash spaces hues by the golden-ratio conjugate, so consecutive /
# nearby alliance ids land far apart on the color wheel (distinct even for a big
# legend), while a FIXED muted saturation + dim value keep every tint a dark
# background wash. Dim is load-bearing: the renderer blits these additively
# (BLEND_RGB_ADD), so overlapping same/other-alliance blobs SUM -- a bright base
# color would wash a dense region to white (the hard-won phase-1 lesson). We use
# explicit arithmetic (golden-ratio multiply + colorsys), never hash(): Python
# randomizes str hashing per run, so hash() would repaint every alliance a
# different color each launch.
_SOV_GOLDEN = 0.618033988749895   # golden-ratio conjugate: hue increment per id
_SOV_SAT = 0.55                   # muted saturation (background wash, not a node)
_SOV_VAL = 0.40                   # dim value: max channel ~102/255 so additive
                                  # stacking of neighbor blobs can't wash to white


def parse_sov_map(payload):
    """Parse the ESI ``/sovereignty/map/`` payload into ``{system_id: alliance_id}``
    for the sovereignty tint layer (Task 33). The endpoint returns a list of
    ``{system_id, alliance_id?, corporation_id?, faction_id?}`` entries; ONLY
    entries carrying an ``alliance_id`` are kept -- corp-only, faction-only and
    unclaimed systems are dropped, because the tint is an ALLIANCE wash. Malformed
    rows (non-dict, missing ``system_id``) are skipped. Pure/testable."""
    out: dict[int, int] = {}
    for r in payload or ():
        try:
            sid = r.get("system_id")
            aid = r.get("alliance_id")
        except AttributeError:
            continue                           # non-dict row -> skip
        if sid is None or aid is None:
            continue
        try:
            out[int(sid)] = int(aid)
        except (TypeError, ValueError):
            continue
    return out


def sov_color(alliance_id) -> tuple[int, int, int]:
    """Deterministic dim tint ``(r, g, b)`` for an alliance id (Task 33). Hue is
    spaced by the golden-ratio conjugate (``(id * 0.618033988749895) % 1``) so
    consecutive / nearby ids land far apart on the color wheel -- distinct even
    across a large legend -- while a FIXED muted saturation and dim value keep
    every tint a dark background wash the additive node glows sit cleanly on top
    of. Arithmetic-only (NO ``hash()`` -- Python randomizes str hashing per run,
    so an alliance would change color every launch), so a given alliance's color
    is byte-identical across runs and sessions."""
    hue = (int(alliance_id) * _SOV_GOLDEN) % 1.0
    r, g, b = colorsys.hsv_to_rgb(hue, _SOV_SAT, _SOV_VAL)
    return (int(r * 255), int(g * 255), int(b * 255))


def canonical_sov(mapping) -> tuple:
    """Canonical hashable signature of a ``{system_id: alliance_id}`` sov mapping
    for the render request tuple + ``map_tab._request_sig`` (Task 33): sorted
    ``((system_id, alliance_id), ...)``. Empty / None -> ``()`` so the request
    layer can collapse it to ``None`` with ``or None`` (a sov-off / on-but-empty
    frame is then byte-identical to the pre-sov output and its sig matches,
    keeping duplicate-render suppression sound). Pure/testable."""
    out = [(int(sid), int(aid)) for sid, aid in (mapping or {}).items()]
    out.sort()
    return tuple(out)


def sov_legend_rows(mapping, names=None, top_n: int = 15) -> list:
    """Legend rows for the sov layer (Task 33): the ``top_n`` alliances by
    tinted-system count, each as ``(alliance_id, label, count, (r, g, b))``.
    ``mapping`` is ``{system_id: alliance_id}``; ``names`` is an optional
    ``{alliance_id: name}`` (a missing / unresolved name falls back to the raw id
    string, so the legend still renders when the name resolve failed). Ties break
    by ascending alliance_id for a deterministic order. Pure/testable."""
    counts: dict[int, int] = {}
    for aid in (mapping or {}).values():
        counts[aid] = counts.get(aid, 0) + 1
    names = names or {}
    ordered = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:max(0, top_n)]
    return [(aid, names.get(aid) or str(aid), cnt, sov_color(aid))
            for aid, cnt in ordered]
