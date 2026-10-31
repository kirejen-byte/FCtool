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
   The gate and the patterns behind it are both case-SENSITIVE, so the gate can
   never be stricter than its regex (a gate stricter than its pattern is a
   silent filter -- the rule gamelog_monitor records above its own gate).

3. **Player vs NPC is bracket presence in the attacker segment.**  A player
   renders as ``Ship [CORP] [ALLY] [Pilot]``; an NPC is a bare name.  That is a
   far cleaner signal than the damage classifier's weapon/charge heuristics,
   and the brackets survive tag stripping.

NOTE on ``_TAG_RE``: it is deliberately defined HERE rather than imported from
``gamelog_monitor``, which owns the identical pattern.  ``gamelog_monitor``
imports this module (it dispatches the events), so importing it back would be
a circular import -- and it computes ``app_dir()`` at import time, which would
make this module do filesystem I/O just to be loaded.  If the two are ever
unified, the direction must be ``gamelog_monitor`` -> ``ewar_parse``, never the
reverse.
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


# ---------------------------------------------------------------------------
# Cheap gate -- raw substrings only, before any regex
# ---------------------------------------------------------------------------
_GATE_JAM = "jammed"
_GATE_TACKLE = "attempt"
_GATE_NEUT = "energy neutralized"
_GATE_NOS = "energy drained"


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
    """``"player"`` when the attacker segment carries a bracket group, else
    ``"npc"``.  A player renders ``Ship [CORP] [ALLY] [Pilot]``; an NPC is a
    bare name.  Character, corporation and ship names cannot contain brackets,
    so presence alone is sufficient.

    A BLANK attacker segment (the handful of bomb / off-grid lines described in
    :func:`_fields_of`) therefore reads ``"npc"`` and is suppressed downstream.
    That is the quiet direction, chosen on purpose: the whole icon set exists
    to be trustworthy when empty, and the measured population is 9 lines in
    2.76 million."""
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
    except (TypeError, ValueError):       # unreachable via the pattern; cheap
        return (m.group("colour").lower(), 0)
    return (m.group("colour").lower(), amount)


def parse_ewar_line(line: str):
    """Return an :class:`EwarHit` for an INCOMING EWAR line, else ``None``.

    Outgoing lines, third-party lines (neither party is the tailed character),
    ``(notify)`` lines that merely mention the vocabulary -- the archive's
    ``"(notify) Some drones were unable to engage with your target due to
    being target jammed"`` is the standing example -- truncated lines and
    garbage all yield ``None``.  Never raises."""
    if not line or not _ewar_gate(line):
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
