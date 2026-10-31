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
loudest event this feature exists to surface must never be demoted.  A cased
``"NPC"`` is therefore a player too -- the parser emits lower case, and
guessing at a variant it does not emit would be the unsafe direction.

===========================================================================
THE REPLAY GUARD (spec 10.3, corrected twice on 2026-10-30)
===========================================================================

The gamelog tailer re-reads a rotated or truncated file FROM POSITION ZERO and
replays it.  Windows expire on arrival time, so an unguarded replay lights
every icon on a tile at once for up to 30 s -- the one failure this feature
cannot have.  Two earlier rules were implemented faithfully and measured not to
work; both corrections are recorded here because each looks like
over-engineering without its counter-example.

**What does not work (correction 1):** comparing log stamps only to EACH
OTHER.  A replayed file is internally consistent and monotonically increasing,
so its newest lines always look fresh relative to the ones before them.  A
three-hour-old fight replayed after a truncation lit four icons on a calm tile.

**The rule.**  Log-stamp space is ANCHORED to the monotonic clock:

  - ``_newest`` is the newest accepted stamp and ``_newest_at`` the monotonic
    reading when it was accepted -- together, one point of correspondence
    between the two clocks.
  - a line's EXPECTED arrival is ``_newest_at + (stamp - _newest)``;
    it is accepted only while ``abs(now - expected) <= _REPLAY_SKEW_S``.
  - the anchor advances on every ACCEPTED stamped line, in either direction.
    Forward-only advance was half of the original bug: one line stamped ahead
    of real time made every later genuine line read as stale, for the rest of
    the session, silently.
  - the hold is armed from the line's OWN AGE:
    ``until = now + window - max(0, now - expected)``.  That subtrahend is
    zero for a line arriving in step with the clock, so the normal path is
    unchanged; a line two seconds late gets two seconds less icon.

Only deltas on each clock are ever compared -- never one clock's reading
against the other's -- so this needs no agreement between EVE's UTC stamps and
the Windows clock, and a mis-set system clock cannot break it.

**THE ANCHOR IS TRACKER-GLOBAL, NOT PER CHARACTER (correction 2).**  A
per-character anchor was the first attempt, and the cost was measured rather
than reasoned about: a replay that is a character's FIRST EWAR of the session
has no anchor to be judged against, and it arms FOUR icons at their FULL 30 s
window with the border red -- the whole of anti-goal 4, not the "last few
seconds" an earlier draft of the spec claimed.  The arithmetic is the part
worth keeping: in a fast-forward replay each line arrives EARLY against the
anchor its predecessor just set, and age-based arming only trims LATE lines,
so it trims nothing at all.  Every stamp gap from 1 s to 120 s armed at least
one full window.

Every EVE client on the machine stamps its logs from the same clock, so one
anchor is sound for all of them -- and a character's first line is then judged
against an anchor that already exists.  The alternative, arming nothing on a
first line, was rejected: it throws away a genuine scramble icon, and tackle
logs once and never again.

**RE-ANCHOR AFTER SUSTAINED REJECTION.**  Bounding the future side introduced a
failure of its own, and a worse one: a wall-clock STEP larger than the skew --
NTP step correction, a manual clock fix, a VM resume, a dual-boot UTC mismatch
-- makes every later line read as out of step, and a rejected line never
re-anchors, so the feature went silent permanently and with no symptom.  (The
rule this replaced survived a forward step, so that was a regression, not a
trade.)  So: when every stamped line has been rejected for more than
:data:`_REPLAY_SKEW_S` of MONOTONIC time, the next one re-anchors instead.
This separates the two cases by the shape of their rejections rather than by
guesswork -- a replay's rejections arrive as a burst within a second or two and
never reach the threshold, while a clock step's persist and do.  The normal
path is untouched: one accepted line clears the streak.

**AND ARM NOTHING FOR ONE SKEW WINDOW AFTER RE-ANCHORING** (:data:`EwarTracker.
_quarantine_until`).  Without this, the re-anchor rule hands the whole hole
straight back, and by an EASIER route than the residual it was meant to narrow:
one rejected stamped line, with no accepted one for a skew window afterwards,
lets the NEXT batch re-anchor itself and then be believed in full -- four icons
at full windows with red armed, and, the anchor being global, every other
client dragged onto an hours-old stamp and silenced too.  It needs no first-line
coincidence, and incoming EWAR is rare enough (9,187 lines in 2.67 M) that an
unbroken streak can persist for hours.  A replay batch arrives within
milliseconds and cannot outlive the quarantine; a clock step pays one extra
skew window, once, taking its outage from roughly 63 s to roughly 123 s -- the
obviously right trade against a tile that lies.

Arming nothing for the re-anchoring LINE alone was measured and does NOT close
it: the lines behind it are then accepted against the new anchor with age 0.
So the quarantine is a window, not a line, and the re-anchoring line falls
inside its own quarantine by construction.  Lines keep anchoring throughout --
only arming is suppressed -- so the stream is re-synchronised by the time it
expires, and holds armed BEFORE it are genuine and left alone.

An unparsable stamp is still ACCEPTED, with age 0, and leaves both the anchor
and the rejection streak untouched: the guard only ever compares log timestamps
to each other, so it has no opinion when it cannot read one, and a line it
cannot read is not evidence either way about the stream.  Dropping instead
would silence the feature outright on any client whose stamp format this parser
misses.

**TWO RESIDUALS, accepted rather than fixed.**

  1. IRREDUCIBLE: the session's VERY FIRST stamped line has nothing to be
     judged against, so it becomes the anchor and arms.  A replay can
     therefore light icons only by arriving as the whole session's first EWAR
     -- which needs a tracked file to rotate before any client has logged a
     single incoming effect, and the monitor seeds new files at EOF so a cold
     start never backfills.  The alternative, arming nothing on a first line,
     would throw away a genuine scramble icon; tackle logs once and never
     again.  The same hole swallows a first line stamped far ahead of real
     time, which the re-anchor rule then bounds to roughly two skew windows
     rather than the session.
  2. A DESCENDING run of backward stamp hops walks the anchor backwards, and
     TWO hops of 50 s are already enough to start rejecting live traffic --
     which, before the re-anchor rule, meant permanent silence; it is now
     bounded to roughly two skew windows.  This is accepted ON MEASUREMENT,
     not assumption: a scan of 2,669,525 stamped lines across 13,580 archive
     files found ZERO files with any stamp decrease, and zero decreases in the
     9,187-line incoming-EWAR sequence this tracker actually sees.

===========================================================================

NEVER RAISES, for any input.  This is read from fc_gui's per-tile ``try`` on
every FCPreview tick: an exception escaping here retires the tile, and five
failed ticks disable every preview.  So a character key that is not a
non-empty string, a malformed hit, a non-finite ``now`` and an effect outside
:data:`ewar_parse.ICON_ORDER` are each dropped in silence rather than coerced
onto some other key, and the readers answer "nothing live" for a ``now`` they
cannot use.  Two guards are deliberately broader than they look:

  - the hit is read behind ``except Exception``, not ``except
    AttributeError``.  The realistic shape change is a dataclass that grows a
    COMPUTED field, whose property raises something else entirely, and that is
    precisely the case the guard exists for.
  - a ``str`` SUBCLASS key is normalised to an exact ``str`` before it reaches
    any dict here (see :func:`_as_key`), because an overridden ``__hash__`` or
    ``__eq__`` raises INSIDE this module's own lookups, where no caller's
    ``try`` is watching.

``cycle_ms_for`` carries the same never-raises contract, for the same reason,
for module names and amounts.

BOUNDED MEMORY.  At most :data:`_MAX_CHARS` character records; seven hold
entries per record are reachable (one per effect for a player, plus NPC
scramble and disruption -- the other three NPC effects are never stored),
within an ``(effect, cls)`` key space of ten.  A record is created only when a
hold is actually stored, so a rejected, dropped or fully-aged line costs no
memory at all.  A record with no live hold that has seen no stored hold for
:data:`_IDLE_RETENTION_S` is dropped when the next new character arrives, and
the cap behind it evicts a record with no live hold before one with (see
:meth:`EwarTracker._prune`).

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

#: Serves two jobs, deliberately with one number.  (1) How far a line's
#: arrival may differ from what the clock anchor predicts, in either
#: direction, before it is rejected -- generous, because a genuine line is at
#: most a poll interval out while a replay is minutes to hours out.  (2) How
#: long an unbroken run of rejections may last before the next line
#: re-anchors.  One number because the two are the same question asked of a
#: line and of a stream: anything that stays out of step for longer than a
#: line is allowed to be out of step indicts the anchor, not the stream.
_REPLAY_SKEW_S = 60

#: Memory bounds -- see the module docstring.
_MAX_CHARS = 64
_IDLE_RETENTION_S = 300.0

_EFFECTS = frozenset(ICON_ORDER)

#: "YYYY.MM.DD HH:MM:SS" as the gamelog writes it.  Anchored at the start and
#: deliberately not at the end: the caller passes the stamp the tailer read off
#: the line, and trailing content must not make it unreadable.
_TS_RE = re.compile(r"\s*(\d{4})\.(\d{1,2})\.(\d{1,2})\s+(\d{1,2}):(\d{1,2}):(\d{1,2})")


def _as_key(char_key):
    """The character key to store under, or None when there is none usable.

    ``""`` is a REAL, reachable listener state (a headerless gamelog file) and
    keying holds on it would smear one file's EWAR across every character that
    also resolved to ``""`` -- so it is dropped rather than accepted, and
    nothing is ever coerced onto a substitute key.

    A ``str`` SUBCLASS is normalised to an exact ``str``, because an overridden
    ``__hash__`` or ``__eq__`` raises inside this module's own dict lookups --
    an ``__eq__`` that raises only needs to COLLIDE with a stored key to get
    there, so probing ``hash()`` alone does not cover it.  ``str.__str__`` is
    called unbound so that an overridden ``__str__`` cannot intercept it, and
    it returns an exact copy for a subclass instance.  The ``hash()`` probe
    stays as the backstop for anything this reasoning missed.
    """
    if not isinstance(char_key, str):
        return None
    try:
        key = char_key if type(char_key) is str else str.__str__(char_key)
        # The emptiness test lives INSIDE the guard and AFTER normalisation:
        # `not key` runs __bool__ / __len__, so on a subclass it is one more
        # place the caller's object gets to raise inside this module.
        if type(key) is not str or not key:
            return None
        hash(key)
    except Exception:
        return None
    return key


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
    or None when the object cannot yield one.

    Duck-typed rather than isinstance-checked so a test double works, and
    guarded broadly because the caller's object comes from a parser fc_gui
    wires by callback: a shape change there must degrade to "no icon", never
    to a retired tile.  The membership test lives INSIDE the guard because an
    unhashable ``effect`` raises on it.
    """
    try:
        effect = hit.effect
        if effect not in _EFFECTS:
            return None
        cls = "npc" if hit.source == "npc" else "player"
        return effect, cls, hit.module, hit.amount
    except Exception:
        return None


def _window_s_for(effect: str, module, amount) -> float:
    """The hold length in seconds for one hit.  Always positive and at most
    :data:`_WINDOW_CEILING_S`; never raises, because ``cycle_ms_for`` never
    does and returns a positive int."""
    if effect in _TACKLE_EFFECTS:
        return TACKLE_WINDOW_S
    raw = cycle_ms_for(module, amount) / 1000.0 * _WINDOW_FACTOR
    return max(_WINDOW_FLOOR_S, min(_WINDOW_CEILING_S, raw))


class _CharState:
    """One character's holds.

    ``holds`` maps ``(effect, cls)`` to the monotonic time the hold ends;
    ``last_add`` is the idle prune's clock, and is stamped only when a hold is
    actually stored, so a rejected line cannot keep a record alive.  The clock
    anchor is NOT here -- it is tracker-global (see the module docstring).
    """

    __slots__ = ("holds", "last_add")

    def __init__(self, now: float):
        self.holds: dict[tuple[str, str], float] = {}
        self.last_add = now

    def live(self, now: float) -> dict[tuple[str, str], float]:
        """``holds`` with every expired entry dropped first.

        Strict ``now < until``: a hold ends AT ``until``, matching
        ``damage_flash._hold_live``.  Pruning on read is what keeps the dict
        bounded without a second sweep -- and is why a read past a window is
        destructive, so the ORDER of probes in a test is load-bearing.
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
        # The clock anchor, shared by every character on this machine (see the
        # module docstring); the monotonic time the current unbroken run of
        # rejections began -- None whenever the last stamped line was
        # accepted; and the monotonic deadline until which nothing may arm
        # after a re-anchor -- None when not quarantined.
        self._newest = None
        self._newest_at = 0.0
        self._rejecting_since = None
        self._quarantine_until = None

    # --- ingest -------------------------------------------------------------

    def add(self, char_key: str, hit, log_ts: str, now: float) -> None:
        """Record one parsed incoming-EWAR line.  ``log_ts`` is the line's own
        timestamp ("YYYY.MM.DD HH:MM:SS") and ``now`` a monotonic float.

        Silently drops anything it cannot use -- see the module docstring.
        """
        key = _as_key(char_key)
        if key is None:
            return
        stamped = _finite(now)
        if stamped is None:
            return
        parts = _hit_parts(hit)
        if parts is None:
            return
        effect, cls, module, amount = parts

        # The replay guard runs BEFORE the NPC drop so that an NPC line still
        # advances the anchor: more lines anchoring it makes the guard
        # tighter, and a dropped line's stamp is no less true.  It also runs
        # before the record lookup, which the global anchor is what allows --
        # so a rejected or dropped line creates no record at all.
        age = self._age_or_reject(log_ts, stamped)
        if age is None:
            return
        # A re-anchor quarantines arming for one skew window, and the line
        # that re-anchored is inside it: a re-anchored batch must not be
        # believed just because it agrees with the anchor it brought with it.
        # Lines still ANCHOR throughout (above), so the stream is back in step
        # by the time this expires.
        if self._quarantine_until is not None:
            if stamped < self._quarantine_until:
                return
            self._quarantine_until = None
        if cls == "npc" and effect not in _NPC_EFFECTS_KEPT:
            return

        until = stamped + _window_s_for(effect, module, amount) - age
        if until <= stamped:
            # The line is older than its own window: it anchored, but there is
            # nothing left of it to show.
            return

        record = self._chars.get(key)
        if record is None:
            # Prune before inserting so the cap is an upper bound on what this
            # tracker ever holds, not one it transiently exceeds.
            self._prune(stamped)
            record = self._chars[key] = _CharState(stamped)
        record.last_add = stamped
        previous = record.holds.get((effect, cls))
        # Refresh EXTENDS only: a shorter module's line must never cut a
        # longer one's live hold.
        if previous is None or until > previous:
            record.holds[(effect, cls)] = until

    def _anchor_on(self, stamp, now: float) -> None:
        """Move the anchor onto this line and clear the rejection streak.
        Every acceptance goes through here, so the streak can never outlive
        an accepted line."""
        self._newest = stamp
        self._newest_at = now
        self._rejecting_since = None

    def _age_or_reject(self, log_ts, now: float):
        """The line's age in seconds (0.0 when it arrived in step with the
        clock), or None when the replay guard rejects it.

        This is the whole of the corrected spec 10.3 rule; the module
        docstring carries the reasoning for each branch.
        """
        stamp = _parse_ts(log_ts)
        if stamp is None:
            # No opinion: neither the anchor nor the streak moves.
            return 0.0
        if self._newest is None:
            self._anchor_on(stamp, now)     # the first stamp IS the anchor
            return 0.0
        expected = self._newest_at + (stamp - self._newest).total_seconds()
        if abs(now - expected) > _REPLAY_SKEW_S:
            if self._rejecting_since is None:
                self._rejecting_since = now
                return None
            if now - self._rejecting_since <= _REPLAY_SKEW_S:
                return None
            # Nothing but rejections for longer than a line is allowed to be
            # out of step: the ANCHOR is wrong, not the stream (a clock step,
            # not a replay).  Re-anchor -- and quarantine arming, because on
            # its own this branch believes a replay in full (see the module
            # docstring, W1).
            self._anchor_on(stamp, now)
            until = now + _REPLAY_SKEW_S
            if self._quarantine_until is None or until > self._quarantine_until:
                # Only ever extends: a caller handing back a smaller `now`
                # must not be able to cut a live quarantine short.
                self._quarantine_until = until
            return 0.0
        # Accepted: re-anchor, in EITHER direction (a forward-only anchor lets
        # one future-dated line silence the feature -- see the docstring).
        self._anchor_on(stamp, now)
        return max(0.0, now - expected)

    def _prune(self, now: float) -> None:
        """Drop idle records, then enforce the hard cap.

        Idle = no live hold AND no stored hold for :data:`_IDLE_RETENTION_S`.
        The hold conjunct cannot fire today -- the 30 s window ceiling is well
        inside the 300 s retention -- and is kept only so that raising the
        ceiling cannot silently start evicting live holds.

        The cap then PREFERS a record with no live hold, so a burst of new
        keys does not evict a character whose icons are on screen.  It is a
        preference and not a guarantee: when every record is live the
        oldest-inserted one goes anyway, because the bound must hold.
        """
        for key in [k for k, record in self._chars.items()
                    if not record.live(now)
                    and now - record.last_add > _IDLE_RETENTION_S]:
            del self._chars[key]
        while len(self._chars) >= _MAX_CHARS:
            victim = next((k for k, record in self._chars.items()
                           if not record.live(now)), None)
            if victim is None:
                victim = next(iter(self._chars))
            del self._chars[victim]

    # --- read ---------------------------------------------------------------

    def _live_holds(self, char_key, now):
        """The live ``(effect, cls)`` holds for a character, or an empty dict.

        Reads without creating a record, so the border painter asking about a
        character that has never been touched by EWAR costs nothing.
        """
        key = _as_key(char_key)
        if key is None:
            return {}
        stamped = _finite(now)
        if stamped is None:
            # A `now` we cannot use reads as "nothing live": the silent
            # direction, and unreachable from the tick in any case.
            return {}
        record = self._chars.get(key)
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
