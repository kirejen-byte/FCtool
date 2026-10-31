"""Per-character incoming-EWAR hold tracker (pure).  All time is
caller-supplied (``now``, a monotonic float) so tests inject a clock; no
wall-clock reads, no I/O, no Tk, no logging here.

One hold per ``(character, effect, source class)``, refreshed by every
qualifying line.  :meth:`EwarTracker.icons` is the icon bar's reader and
:meth:`EwarTracker.red_live` the border painter's; both take the same ``now``
the preview tick already has.

WINDOW per hit (design A1) -- ``cycle_ms_for(module, amount) / 1000 * 1.25``,
clamped to ``[_WINDOW_FLOOR_S, _WINDOW_CEILING_S]`` = 6 s to 30 s.  The pieces
are each load-bearing:

  - the 1.25 multiplier absorbs the 1 s poll interval plus one late or dropped
    line without inventing a second mechanism;
  - the 30 s CEILING is what tames the Standup Energy Neutralization Burst
    Projector's 480 s cycle -- an eight-minute neut icon is indistinguishable
    from a stuck one, and nothing on a tile may outlive 30 s of silence;
  - the 6 s FLOOR stops a small nosferatu (2.5 s cycle, 3.1 s window) from
    flickering faster than the eye settles.

TACKLE is the exception: ``scramble`` and ``disruption`` take a flat
:data:`TACKLE_WINDOW_S` and never consult a cycle, because tackle logs ONCE at
activation and never again (design D3).  There is no second line to refresh
from, so a cycle-derived window would clear the icon while the point still
holds.  A refresh only ever EXTENDS a hold (``max``), never shortens it: two
neuts stacking must not let the small one's 7.5 s cut the heavy one's 30 s.

SOURCE RULES (design D4/D9) -- the asymmetry is deliberate and looks like a
bug without the why:

  - NPC ``jam`` / ``neut`` / ``nos`` are DISCARDED OUTRIGHT.  Rats and Sansha
    neut relentlessly; an icon that is usually on is worse than no icon.
  - NPC ``scramble`` / ``disruption`` ARE KEPT.  NPC tackle is as trapping as
    a player's, and the archive says NPC tackle is almost entirely scramble.
  - :meth:`red_live` is True only for a PLAYER-sourced live hold, so NPC
    tackle draws an icon and leaves the border to the damage tracker.

Only the exact string ``"npc"`` is an NPC, matching ``damage_flash``'s
fail-safe direction: an unclassifiable attacker counts as a player, because
``ewar_parse`` already resolves an empty attacker segment that way and the
loudest event this feature exists to surface must never be demoted.

THE REPLAY GUARD (spec 10.3).  The gamelog tailer re-reads a rotated or
truncated file FROM POSITION ZERO and replays it.  Windows expire on arrival
time, so an unguarded replay would light every icon on a tile at once for up
to 30 s -- the one failure this feature cannot have.  So :meth:`add` takes the
line's own log timestamp and discards any event more than
:data:`_REPLAY_SKEW_S` seconds behind the newest stamp seen FOR THAT
CHARACTER.  It needs no clock-skew reasoning because it only ever compares log
timestamps to each other -- and therefore has NO OPINION when it cannot read
one: an unparsable stamp is ACCEPTED, not dropped.  Dropping would silence the
feature outright on any client whose stamp format this parser misses.

NEVER RAISES, for any input.  This is read from fc_gui's per-tile ``try`` on
every FCPreview tick: an exception escaping here retires the tile, and five
failed ticks disable every preview.  So a non-string character key, a
malformed hit, a non-finite ``now`` and an effect outside
:data:`ewar_parse.ICON_ORDER` are each dropped in silence rather than
coerced onto some other key, and the readers answer "nothing live" for a
``now`` they cannot use.  ``cycle_ms_for`` carries the same contract for
module names and amounts.

BOUNDED MEMORY.  At most :data:`_MAX_CHARS` character records, each holding at
most one entry per (effect, class) -- ten.  A record with no live hold that has
seen no event for :data:`_IDLE_RETENTION_S` is dropped when the next new
character arrives; the cap is the backstop against a key that churns (a
rename, a mis-resolved listener).

Threading: every caller is on the Tk thread (ingest via fc_gui's ``_post_ui``,
reads via the preview tick), so the unlocked mutations here are
single-threaded BY THAT INVARIANT -- a future off-thread caller must add
locking."""
from __future__ import annotations

import math
import re
from collections import OrderedDict
from datetime import datetime

from ewar_cycles import cycle_ms_for
from ewar_parse import ICON_ORDER

#: Flat window for scramble and disruption (design D3) -- see the module
#: docstring.  Equal to the ceiling below on purpose: nothing on a tile ever
#: outlives 30 s of silence.
TACKLE_WINDOW_S = 30.0

#: The two effects that log once at activation, and the two whose NPC-sourced
#: lines are kept.  The same pair for both reasons, coincidentally: tackle is
#: both the effect with no usable cycle and the effect an NPC can inflict
#: meaningfully.
_TACKLE_EFFECTS = frozenset(("scramble", "disruption"))
_NPC_EFFECTS_KEPT = _TACKLE_EFFECTS

_WINDOW_FACTOR = 1.25
_WINDOW_FLOOR_S = 6.0
_WINDOW_CEILING_S = 30.0

#: How far behind the newest log stamp seen for a character an event may be
#: before it is read as a replay.  Generous on purpose: a legitimate line is
#: at most a poll interval late, while a replay is minutes to hours behind.
_REPLAY_SKEW_S = 60

#: Memory bounds -- see the module docstring.
_MAX_CHARS = 64
_IDLE_RETENTION_S = 300.0

_EFFECTS = frozenset(ICON_ORDER)

#: "YYYY.MM.DD HH:MM:SS" as the gamelog writes it.  Anchored at the start and
#: deliberately not anchored at the end: the caller passes the stamp the
#: tailer read off the line, and trailing content must not make it unreadable.
_TS_RE = re.compile(r"\s*(\d{4})\.(\d{1,2})\.(\d{1,2})\s+(\d{1,2}):(\d{1,2}):(\d{1,2})")


def _finite(value):
    """``float(value)`` when ``value`` is a finite real number, else None.

    Stricter than ``damage_flash.finite_or`` on purpose: this coerces ``now``,
    which only ever arrives from ``time.monotonic()``.  A string that happens
    to parse as a number is a caller bug, not a hand-edited config value, so
    there is nothing to be lenient about -- and a bool is rejected for the
    same reason (``True`` is not a timestamp).
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _parse_ts(log_ts):
    """The log stamp as a naive datetime, or None when it cannot be read.

    Two stamps are only ever subtracted from each other, so the absence of a
    timezone is not a gap: the difference is exact either way, and a naive
    datetime cannot be accidentally compared against a wall clock.
    """
    if not isinstance(log_ts, str):
        return None
    match = _TS_RE.match(log_ts)
    if match is None:
        return None
    try:
        return datetime(*[int(part) for part in match.groups()])
    except (TypeError, ValueError, OverflowError):
        # A syntactically fine stamp can still be an impossible date
        # ("2026.13.45 99:99:99"); that reads as unparsable, i.e. accepted.
        return None


def _hit_parts(hit):
    """``(effect, cls, module, amount)`` from an :class:`ewar_parse.EwarHit`,
    or None when the object is not one.

    Duck-typed rather than isinstance-checked so a test double works, and
    wrapped because the caller's object comes from a parser that fc_gui wires
    by callback -- a shape change there must degrade to "no icon", never to a
    retired tile.
    """
    try:
        effect = hit.effect
        source = hit.source
        module = hit.module
        amount = hit.amount
    except AttributeError:
        return None
    # EXACT match on "npc" only: "", None, "NPC" and anything else are
    # PLAYER, the fail-safe direction (see the module docstring).
    return effect, ("npc" if source == "npc" else "player"), module, amount


def _window_s_for(effect: str, module, amount) -> float:
    """The hold length in seconds for one hit.  Always positive and at most
    :data:`_WINDOW_CEILING_S`; never raises, because ``cycle_ms_for`` never
    does and returns a positive int."""
    if effect in _TACKLE_EFFECTS:
        return TACKLE_WINDOW_S
    raw = cycle_ms_for(module, amount) / 1000.0 * _WINDOW_FACTOR
    return max(_WINDOW_FLOOR_S, min(_WINDOW_CEILING_S, raw))


class _CharState:
    """One character's holds plus its own replay clock.

    ``holds`` maps ``(effect, cls)`` to the monotonic time the hold ends, so
    it carries at most ten entries.  ``newest`` is the newest log timestamp
    accepted for this character (the replay guard's only state) and
    ``last_add`` the monotonic time of its last event (the idle prune's).
    """

    __slots__ = ("holds", "newest", "last_add")

    def __init__(self, now: float):
        self.holds: dict[tuple[str, str], float] = {}
        self.newest = None
        self.last_add = now

    def live(self, now: float) -> dict[tuple[str, str], float]:
        """``holds`` with every expired entry dropped first.

        Strict ``now < until``: a hold ends AT ``until``, matching
        ``damage_flash._hold_live``.  Pruning on read is what keeps the dict
        bounded without a second sweep.
        """
        for key in [k for k, until in self.holds.items() if not now < until]:
            del self.holds[key]
        return self.holds


class EwarTracker:
    def __init__(self):
        # Insertion-ordered so the cap evicts the character seen longest ago
        # rather than an arbitrary one.  NOT touched on read: an LRU on reads
        # would make the eviction order depend on which tiles happen to be
        # shown, and a character being read is exactly one worth keeping.
        self._chars: "OrderedDict[str, _CharState]" = OrderedDict()

    # --- ingest -------------------------------------------------------------

    def add(self, char_key: str, hit, log_ts: str, now: float) -> None:
        """Record one parsed incoming-EWAR line.  ``log_ts`` is the line's own
        timestamp ("YYYY.MM.DD HH:MM:SS") and ``now`` a monotonic float.

        Silently drops anything it cannot use -- see the module docstring.
        """
        if not isinstance(char_key, str) or not char_key:
            # "" is a REAL, reachable listener state (a headerless gamelog
            # file), and keying holds on it would smear one file's EWAR
            # across every character that also resolved to "".
            return
        stamped = _finite(now)
        if stamped is None:
            return
        parts = _hit_parts(hit)
        if parts is None:
            return
        effect, cls, module, amount = parts
        if effect not in _EFFECTS:
            return

        record = self._chars.get(char_key)
        if record is None:
            # Prune before inserting so the cap below is an upper bound on
            # what this tracker ever holds, not a bound it transiently
            # exceeds.
            self._prune(stamped)
            record = self._chars[char_key] = _CharState(stamped)
        record.last_add = stamped

        # The replay guard runs BEFORE the NPC drop so that an NPC line still
        # advances this character's clock: more lines advancing it makes the
        # guard tighter, and a dropped line's stamp is no less true.
        stamp = _parse_ts(log_ts)
        if stamp is not None:
            if (record.newest is not None
                    and (record.newest - stamp).total_seconds() > _REPLAY_SKEW_S):
                return
            if record.newest is None or stamp > record.newest:
                record.newest = stamp

        if cls == "npc" and effect not in _NPC_EFFECTS_KEPT:
            return

        until = stamped + _window_s_for(effect, module, amount)
        key = (effect, cls)
        previous = record.holds.get(key)
        # Refresh EXTENDS only: a shorter module's line must never cut a
        # longer one's live hold.
        if previous is None or until > previous:
            record.holds[key] = until

    def _prune(self, now: float) -> None:
        """Drop idle records, then enforce the hard cap.

        Idle = no live hold AND no event for :data:`_IDLE_RETENTION_S`; the
        hold check is what stops a character with a 30 s window from being
        evicted mid-hold by a burst of new keys.
        """
        for key in [k for k, record in self._chars.items()
                    if not record.live(now) and now - record.last_add > _IDLE_RETENTION_S]:
            del self._chars[key]
        while len(self._chars) >= _MAX_CHARS:
            self._chars.popitem(last=False)

    # --- read ---------------------------------------------------------------

    def _live_holds(self, char_key, now):
        """The live ``(effect, cls)`` holds for a character, or an empty dict.

        Reads without creating a record, so the border painter asking about a
        character that has never been touched by EWAR costs nothing.
        """
        if not isinstance(char_key, str) or not char_key:
            return {}
        stamped = _finite(now)
        if stamped is None:
            # A `now` we cannot use reads as "nothing live": the silent
            # direction, and unreachable from the tick in any case.
            return {}
        record = self._chars.get(char_key)
        if record is None:
            return {}
        return record.live(stamped)

    def icons(self, char_key: str, now: float) -> tuple[str, ...]:
        """The effects to draw for this character, in
        :data:`ewar_parse.ICON_ORDER` -- never in recency order, so the bar
        never reshuffles under the eye."""
        live = {effect for effect, _cls in self._live_holds(char_key, now)}
        if not live:
            return ()
        return tuple(effect for effect in ICON_ORDER if effect in live)

    def red_live(self, char_key: str, now: float) -> bool:
        """True iff a PLAYER-sourced hold is live for this character.

        NPC tackle therefore shows an icon without reddening the border
        (design D9/A7) -- the border stays whatever the damage tracker says.
        """
        return any(cls == "player" for _effect, cls in self._live_holds(char_key, now))
