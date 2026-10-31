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
  - a JAM takes its exact cycle, no multiplier: the margin is for effects whose
    NEXT line may arrive late, and a jam's end is already known.  A jam line
    means jammed for one cycle, so ECM is 20 s and a ship burst jammer 30 s;
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

STALE LINES ARE STOPPED BY THE TAILER, NOT HERE (spec 10.3).  Windows expire
on arrival time, so every line that reaches :meth:`EwarTracker.add` is
believed.  That is only honest because ``gamelog_monitor`` keeps the EWAR sink
shut for any read of a file that was not already caught up before it: a
relaunch, a rotation, a hidden tile, a re-pointed logs folder.

This module does not re-check, and must not grow a timestamp guard.  The tailer
KNOWS which reads are history; a guard here can only infer it from log stamps.
Five rounds tried that (stamps against each other, per-character and global
clock anchors, re-anchoring, a quarantine).  Each closed a hole and opened a
narrower one, normal use paid for it with two minutes of machine-wide silence
after a wall-clock step, and a relaunch still lit a tile with a finished fight.

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
hold is actually stored, so a dropped line costs no memory at all.  A record with no live hold that has seen no stored hold for
:data:`_IDLE_RETENTION_S` is dropped when the next new character arrives, and
the cap behind it evicts a record with no live hold before one with (see
:meth:`EwarTracker._prune`).

Threading: every caller is on the Tk thread (ingest via fc_gui's ``_post_ui``,
reads via the preview tick), so the unlocked mutations here are
single-threaded BY THAT INVARIANT -- a future off-thread caller must add
locking."""
from __future__ import annotations

import math
from collections import OrderedDict

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

#: Memory bounds -- see the module docstring.
_MAX_CHARS = 64
_IDLE_RETENTION_S = 300.0

_EFFECTS = frozenset(ICON_ORDER)


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
    raw = cycle_ms_for(module, amount) / 1000.0
    if effect != "jam":
        raw *= _WINDOW_FACTOR
    return max(_WINDOW_FLOOR_S, min(_WINDOW_CEILING_S, raw))


class _CharState:
    """One character's holds.

    ``holds`` maps ``(effect, cls)`` to the monotonic time the hold ends;
    ``last_add`` is the idle prune's clock, and is stamped only when a hold is
    actually stored, so a dropped line cannot keep a record alive.
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

    # --- ingest -------------------------------------------------------------

    def add(self, char_key: str, hit, now: float) -> None:
        """Record one parsed incoming-EWAR line arriving at ``now``, a
        monotonic float.

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
        if cls == "npc" and effect not in _NPC_EFFECTS_KEPT:
            return

        until = stamped + _window_s_for(effect, module, amount)
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
