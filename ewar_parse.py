"""Pure parser for INCOMING electronic- and capacitor-warfare gamelog lines.

English client only, like every other gamelog parser in this project.  No I/O,
no Tk, no clock, no logging: one function over one string, so it can be
exercised against the real archive lines in ``tests/test_ewar_parse.py``
without a harness.  It runs on the gamelog POLL thread.

Five effects are detectable at all.  Sensor dampeners, webs, painters and the
disruptor family write NOTHING to the victim's log -- EVE logs effects that
interrupt you, not effects that degrade you -- so they are permanently out of
reach and this module does not pretend otherwise.

Three things here are load-bearing and look like detail:

1. **Direction for energy neutralization is the COLOUR, read before the markup
   is stripped.**  Incoming and outgoing neut lines are textually identical;
   they differ only in the colour attribute on the amount (incoming
   ``0xffe57f7f``, outgoing ``0xff7fffff``).  Outgoing lines outnumber incoming
   9,139 to 4,356 in the local archive, so a parser that strips tags first and
   then matches text lights every tile from the owner's own neutralizers --
   permanently, and backwards.  Where plain text CAN decide the direction
   (``You're jammed by``, ``to you!``, ``drained to`` / ``drained from``) the
   text is used instead, because text survives a client colour change.
   Neut has no such option, so an unreadable colour on a neut line fails
   CLOSED: a missing icon beats a wrong one.

2. **The cheap gate.**  The poll loop runs every second; an unconditional
   regex per line is not acceptable.  :func:`_ewar_gate` is four raw substring
   tests and nothing else, in the shape of ``gamelog_monitor._cyno_gate``.
   Both the gate and the patterns behind it are case-SENSITIVE, which removes
   the obvious way for a gate to end up stricter than its pattern.  It is not a
   proof of agreement, and the claim is deliberately not made here: the words
   are matched on the RAW line and the patterns on the STRIPPED one, so a
   client that doubled a space or pushed a tag through the middle of a phrase
   could in principle pass a pattern the gate rejects, and such a gate is a
   SILENT filter rather than a loud failure (the rule gamelog_monitor records
   above its own gate).  What is measured: across 2,760,250 archive lines
   there is no line the patterns accept and the gate rejects, and the gate
   words are single tokens wherever a single token will do, so no phrase-
   internal whitespace or tag can split one.

3. **Player vs NPC is read off the attacker segment, in three cases.**  A
   player renders as ``Ship [CORP] [ALLY] [Pilot]``; an NPC is a bare name; and
   a segment carrying no name at all is UNCLASSIFIABLE, which counts as a
   player.  See :func:`_source_of` -- the third case is the one that is easy to
   get wrong, and getting it wrong drops the single loudest event this feature
   exists to surface.

NOTE on ``_TAG_RE``: the duplication of ``gamelog_monitor._TAG_RE`` below is
DELIBERATE and reviewed -- please do not "fix" it by importing that one.
``gamelog_monitor`` imports THIS module (it dispatches the events), so importing
it back is a circular import that fails in both directions: that module defines
its own ``_TAG_RE`` far below its import block, so whichever of the two is
loaded first, the other gets a half-initialised module.  Even without the
cycle, importing it would cost this module its independence: measured, the
import pulls in dozens of modules (86 ``sys.modules`` entries), CONFIGURES THE
ROOT LOGGER and opens ``fctool.log``, none of which a line parser should need
to be loaded or tested.  Re-pointing ``gamelog_monitor`` at this module instead
was considered and REJECTED (2026-10-30): it would put three working parsers --
damage, decloak, cyno -- behind a new cross-module dependency in order to
deduplicate a ten-character regex.  Keep both.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# The canonical effect tuple AND the fixed left-to-right display order of the
# icon bar: ordered by how much the effect constrains you (can't leave, can't
# fight, can't run modules), never by recency, so the bar never reshuffles
# under the eye.  The tracker and the icon sheet import this list rather than
# each keeping their own.
ICON_ORDER = ("scramble", "disruption", "jam", "neut", "nos")


@dataclass(frozen=True)
class EwarHit:
    """One incoming EWAR line, parsed.  Carries no timestamp on purpose: the
    tailer knows the line's timestamp and the character it belongs to, and
    passes both alongside the hit."""
    effect: str        # one of ICON_ORDER
    source: str        # "player" | "npc"
    module: str        # module name verbatim, "" when the line names none
    amount: int = 0    # GJ for neut/nos, 0 otherwise -- feeds the cycle fallback

    # Two things about `module` a consumer should know.  On an NPC capacitor-
    # warfare line the client repeats the SHIP name where a player line names
    # the module, so `module` is a ship name there; harmless, because NPC
    # capacitor warfare is discarded before any cycle lookup.  And it is NOT
    # ASCII-safe: it is client text passed through verbatim, and one archive
    # value carries U+2666 (a diamond-marked NPC hull).  Anything that logs or
    # encodes it must not assume ASCII.


# ---------------------------------------------------------------------------
# Cheap gate -- raw substrings only, before any regex
# ---------------------------------------------------------------------------
_GATE_JAM = "jammed"
_GATE_TACKLE = "attempt"
# Single tokens, not the two-word phrases: "energy neutralized" would be
# defeated by a doubled space or by a tag pushed between the words, and a gate
# that rejects a line its pattern accepts is a silent filter.  Neither variant
# occurs in the archive, so this closes a gap rather than a live bug, and it
# costs nothing measurable.  `attempt` stays a bare word for the same reason it
# is not "attempt from": the client writes `attempt</b> ... <font ...>from`, so
# the two-word form would lose every one of the 1,628 tackle hits.
_GATE_NEUT = "neutralized"
_GATE_NOS = "drained"


def _ewar_gate(line: str) -> bool:
    """Cheap "could this be an incoming EWAR line?" test.

    Four substring tests on the RAW line.  Every phrase is contiguous in the
    rendered text, so each one survives tag stripping intact -- which is what
    lets the gate stay on the raw line while the patterns read plain text."""
    return (_GATE_JAM in line
            or _GATE_TACKLE in line
            or _GATE_NEUT in line
            or _GATE_NOS in line)


# ---------------------------------------------------------------------------
# Patterns
# ---------------------------------------------------------------------------
# Same pattern as gamelog_monitor._TAG_RE -- see the module docstring for why
# it is defined here instead of imported.
_TAG_RE = re.compile(r"<[^>]*>")

# The colour attribute and the signed amount on a capacitor-warfare line, read
# off the RAW line because stripping the markup destroys the colour.  Hex
# digits come in both cases in real lines (0xffe57f7f beside 0xFFFFB900), so
# the capture is case-tolerant and compared lowercased.
_GJ_RE = re.compile(
    r"<color=0x(?P<colour>[0-9a-fA-F]{8})>\s*<b>\s*(?P<amount>[+-]?\d+)\s*GJ\b")

_COLOUR_IN = "ffe57f7f"    # the amount is being taken OFF you
_COLOUR_OUT = "ff7fffff"   # the amount is being taken off someone else

# Plain-text patterns, applied after tag stripping.  ``rest`` deliberately
# CAPTURES the whitespace that follows the effect phrase instead of letting a
# trailing ``\s+`` eat it: on a line whose attacker renders empty the very next
# characters are the field separator, and a consumed space turns " - Module"
# into "- Module", which no longer splits.
_JAM_RE = re.compile(r"You're\s+jammed\s+by(?P<rest>\s.+)$")
_TACKLE_RE = re.compile(
    r"Warp\s+(?P<kind>scramble|disruption)\s+attempt\s+from\s+"
    r"(?P<att>.*?)\s+to\s+you!")
_NEUT_RE = re.compile(r"energy\s+neutralized(?P<rest>\s.+)$")
_NOS_RE = re.compile(r"energy\s+drained\s+(?P<dir>to|from)(?P<rest>\s.+)$")

# The client's field separator.  It is written " - " but the markup collapses
# to a DOUBLED separator on player lines ("[Some Pilot] - - Module"), so the
# dash is matched with a lookahead rather than consuming the trailing space --
# otherwise the second dash lands at the head of the module name.  A module
# name's own hyphens ("Hornet EC-300") carry no space before the dash and are
# therefore untouched.
_SEP_RE = re.compile(r"\s+-(?=\s|$)")

# "Does the attacker segment name anybody at all?"  One word character is
# enough; a segment with none is a name the client never resolved (see
# _source_of), not a short one.
_NAME_CHAR_RE = re.compile(r"\w")


def _fields_of(rest: str):
    """Split the text after the effect phrase into ``(attacker, module)``.

    The attacker is the FIRST field by position and the module the last
    non-empty one after it; ``module`` is ``""`` when the line names none.
    Position matters: 9 archive lines render an EMPTY attacker (a bomb or an
    off-grid source whose name never arrived --
    ``energy neutralized <b><color=..></b> - Void Bomb``), so collapsing empty
    fields first would promote the MODULE into the attacker slot and leave the
    module blank."""
    parts = _SEP_RE.split(rest)
    attacker = parts[0].strip()
    tail = [p.strip() for p in parts[1:]]
    tail = [p for p in tail if p]
    return (attacker, tail[-1] if tail else "")


def _source_of(attacker: str) -> str:
    """``"player"`` / ``"npc"`` for one attacker segment.  Three cases, and
    ONLY the middle one is NPC:

    * **no name at all** -> ``"player"``.  An unclassifiable attacker is never
      quietly downgraded.  This is the same fail-safe direction, and the same
      reasoning, as ``damage_flash._hit_class``, which matches the exact string
      ``"npc"`` and treats ``"unknown"`` / ``None`` / ``""`` as a player.
    * **a bare name** -> ``"npc"``.  That is what an NPC renders as.
    * **a bracket group** -> ``"player"``.  A player renders
      ``Ship [CORP] [ALLY] [Pilot]``, and character, corporation and ship names
      cannot contain brackets, so presence alone is sufficient.

    The first case is not hypothetical and not a tie-break: 9 lines in the
    2.76-million-line archive render an empty attacker, and they are BOMBS --
    ``energy neutralized <b><color=..></b> - Void Bomb``, two of them for 632
    and 773 GJ.  A bomb has no attributable ship at detonation, which is why
    the field is empty; it is player-sourced all the same.  Reading those as
    NPC would suppress the icon (owner decision D4) for the loudest single
    capacitor-warfare event the gamelog contains -- precisely the "see a neut,
    switch to that client and cap-boost" case the feature was asked for.  The
    spec's bracket rule separates a bracketed player from a BARE-NAME NPC; it
    says nothing about an empty field, so this fills the gap rather than
    reading the rule literally."""
    if not _NAME_CHAR_RE.search(attacker):
        return "player"
    return "player" if "[" in attacker else "npc"


def _gj_of(line: str):
    """``(colour, amount)`` off the RAW line: the lowercased 8-hex-digit colour
    attribute on the amount and its magnitude, or ``("", 0)`` when the line
    carries no readable GJ amount.

    The sign is dropped: an incoming nosferatu writes ``-0 GJ`` and the amount
    only ever feeds the cycle-time fallback, which buckets on magnitude."""
    m = _GJ_RE.search(line)
    if not m:
        return ("", 0)
    try:
        amount = abs(int(m.group("amount")))
    except (TypeError, ValueError):
        # REACHABLE, which is why the try is here: the pattern's `\d+` has no
        # length bound, and CPython refuses to convert an integer literal of
        # more than 4,300 digits ("Exceeds the limit (4300 digits) for integer
        # string conversion", a ValueError).  A corrupt or adversarial line
        # therefore yields a readable colour and no amount, rather than
        # throwing out of the poll thread.
        return (m.group("colour").lower(), 0)
    return (m.group("colour").lower(), amount)


def parse_ewar_line(line: str):
    """Return an :class:`EwarHit` for an INCOMING EWAR line, else ``None``.

    Outgoing lines, third-party lines (neither party is the tailed character),
    ``(notify)`` lines that merely mention the vocabulary -- the archive's
    ``"(notify) Some drones were unable to engage with your target due to
    being target jammed"`` is the standing example -- truncated lines and
    garbage all yield ``None``.

    Never raises, for ANY input: the type test below makes that true of a
    non-string too (bytes, an int, a list), which keeps this total in the same
    way ``ewar_cycles.cycle_ms_for`` is.  The live caller only ever passes a
    ``str``, so this is about the function being honest about its contract
    rather than about a reachable bug."""
    if not isinstance(line, str) or not _ewar_gate(line):
        return None
    # Every detectable effect is a (combat) line.  This single test is what
    # keeps the (notify) drone line from drawing a jam icon.
    if "(combat)" not in line:
        return None

    plain = _TAG_RE.sub("", line)

    m = _JAM_RE.search(plain)
    if m:
        # Outgoing jams read "<target> - jammed" and never carry "You're
        # jammed by", so the pattern itself is the direction test.
        attacker, module = _fields_of(m.group("rest"))
        return EwarHit("jam", _source_of(attacker), module)

    m = _TACKLE_RE.search(plain)
    if m:
        # "to you!" is the direction test: outgoing reads "from you to <X>"
        # and a third-party line names neither side as you.
        return EwarHit(m.group("kind"), _source_of(m.group("att")), "")

    m = _NEUT_RE.search(plain)
    if m:
        colour, amount = _gj_of(line)
        if colour != _COLOUR_IN:
            return None            # outgoing, or unreadable: fail CLOSED
        attacker, module = _fields_of(m.group("rest"))
        return EwarHit("neut", _source_of(attacker), module, amount)

    m = _NOS_RE.search(plain)
    if m:
        if m.group("dir") != "to":
            return None            # "drained from" = you are the drainer
        colour, amount = _gj_of(line)
        if colour == _COLOUR_OUT:
            return None            # colour contradicts the text: fail CLOSED
        attacker, module = _fields_of(m.group("rest"))
        return EwarHit("nos", _source_of(attacker), module, amount)

    return None
