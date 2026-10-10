"""Store + environment + pending time + clock -> the Discord ping text (pure: no Tk).

The ping has ONE producer. :func:`compose` is called by the one-click button and by the
editor's preview alike, so what the user is shown is always what gets copied; nothing else
in the app turns a template into ping text.

What goes in:

* the store (:class:`ping_store.PingStore`) - the lists, the picks, the templates, the two
  texts and the When format. It is only read here; composing never writes the config.
* :class:`Env` - what the app knows at that moment and the store deliberately does not keep:
  the selected doctrine, the FC's name, the staging system.
* ``when_unix`` - the pending "When" (``None`` = Now), owned by :class:`WhenSession`.
* ``now_unix`` - the clock, always handed in. This module never reads the time.

Every value is stripped before it reaches :func:`ping_doc.resolve`, which leaves a line out
only when its value is exactly ``""`` - so a name made of blanks counts as no name.

Imports the three sibling ping modules and ``dataclasses`` only; safe from any thread.
"""
from __future__ import annotations

from dataclasses import dataclass

import ping_doc
from discord_time import eve_label, format_when
from ping_store import STAGING

__all__ = ["Env", "Composed", "WhenSession", "values_for", "template_for", "compose",
           "summary", "tooltip_text"]

_DASH = chr(0x2014)          # em dash, built at runtime: the source stays pure ASCII
_DOT = chr(0xB7)             # middle dot
_ELLIPSIS = chr(0x2026)
_NAME_MODE = "name"          # the doctrine pick that shows the doctrine's own name

_NOTHING_TO_COPY = "Nothing to copy %s the template is empty" % _DASH
_WHEN_RESETS = "When resets to Now after this copy"
TOOLTIP_LINE_CHARS = 80      # the widest line a tooltip shows before it is cut


@dataclass(frozen=True)
class Env:
    """What the app knows when a ping is composed. No field is ever stored."""

    doctrine_id: "str | None"
    doctrine_name: str        # "" when none selected
    fc_name: str              # "" when no character
    staging_name: str         # "" when unset


@dataclass
class Composed:
    text: str                 # what goes on the clipboard; "" = nothing to copy
    warnings: list            # human sentences, in document order
    omitted: list             # ping kinds whose line was left out (from PingText.omitted)
    values: ping_doc.PingValues
    when_unix: "int | None"   # the time this text was composed for; None = Now


class WhenSession:
    """The pending "When" of this session - never persisted.

    ``unix`` is ``None`` for Now. A time that was set lasts for exactly one copy: the
    caller composes with ``unix`` and, once the copy has succeeded, calls :meth:`consume`.
    """

    unix: "int | None"

    def __init__(self) -> None:
        self.unix = None

    def set(self, unix) -> None:
        self.unix = None if unix is None else int(unix)

    def consume(self) -> "int | None":
        """Hand back the pending value and go back to Now."""
        unix, self.unix = self.unix, None
        return unix


def _clean(value) -> str:
    """A value as ``resolve`` wants it: stripped text; anything that is not text is ``""``."""
    return value.strip() if isinstance(value, str) else ""


def values_for(store, env: Env, when_unix, now_unix: int) -> ping_doc.PingValues:
    """The six display strings for ``env``'s doctrine, each stripped (``""`` = nothing)."""
    picks = store.picks(env.doctrine_id)
    location = env.staging_name if picks.location == STAGING else picks.location
    if picks.doctrine == _NAME_MODE:
        doctrine = env.doctrine_name
    else:
        doctrine = store.text("doctrine_text")
    when = format_when(when_unix, store.when_format(), now_unix=now_unix,
                       now_text=_clean(store.text("now_text")))
    return ping_doc.PingValues(
        fleet_type=_clean(picks.fleet_type),
        fc=_clean(env.fc_name),
        location=_clean(location),
        when=when,
        doctrine=_clean(doctrine),
        comms=_clean(picks.comms),
    )


def template_for(store, env: Env) -> list:
    """The template in force for ``env``'s doctrine, as RUNS: its own, else the basic one,
    else the built-in one."""
    stored = store.template(env.doctrine_id)
    if stored is None:
        return ping_doc.default_doc()
    return ping_doc.clean_doc(stored)


def compose(store, env: Env, when_unix, now_unix: int, *, doc=None) -> Composed:
    """The ping as it would be copied right now.

    ``doc`` is the editor's live runs and is used as is - an empty list included; without
    one the stored template is used (:func:`template_for`).
    """
    values = values_for(store, env, when_unix, now_unix)
    runs = template_for(store, env) if doc is None else doc
    result = ping_doc.resolve(runs, values)
    return Composed(result.text, list(result.warnings), list(result.omitted), values,
                    when_unix)


def summary(composed: Composed, *, consumed_when, now_unix: int) -> tuple:
    """The status line after a copy: ``(text, "ok" | "warn")``.

    ``consumed_when`` is what :meth:`WhenSession.consume` returned for this copy. An empty
    ping is its own sentence. Otherwise the line is ``Ping copied`` and up to three parts:

    1. the time that was used up - marked ``(already passed)`` when it was behind
       ``now_unix`` - or, with none, the fleet type and ``When: Now``;
    2. ``left out: <Label>, <Label>`` for the fields whose line was dropped;
    3. ``N more warning(s)`` for the warnings part 2 does not account for (an unsupported
       pill, say).

    The level is ``"warn"`` for an empty ping, when part 2 or 3 is there, and for a used-up
    time that had already passed - so a yellow line always says why.
    """
    if not composed.text:
        return _NOTHING_TO_COPY, "warn"
    separator = " %s " % _DOT
    passed = consumed_when is not None and consumed_when < now_unix
    if consumed_when is not None:
        parts = ["When was %s%s; it is back to Now"
                 % (eve_label(consumed_when, now_unix), " (already passed)" if passed else "")]
    else:
        fleet_type = composed.values.fleet_type
        parts = [fleet_type, "When: Now"] if fleet_type else ["When: Now"]
    level = "warn" if passed else "ok"
    omitted = list(composed.omitted or ())
    if omitted:
        parts.append("left out: " + ", ".join(
            ping_doc.FIELD_LABELS.get(kind, str(kind)) for kind in omitted))
        level = "warn"
    more = len(composed.warnings or ()) - len(omitted)
    if more > 0:
        parts.append("1 more warning" if more == 1 else "%d more warnings" % more)
        level = "warn"
    return "Ping copied %s %s" % (_DASH, separator.join(parts)), level


def _cut(text: str, limit: int) -> str:
    """``text`` in at most ``limit`` characters (at least one); a cut ends in an ellipsis."""
    limit = max(1, limit)
    return text if len(text) <= limit else text[:limit - 1] + _ELLIPSIS


def tooltip_text(composed: Composed, *, pending: bool, max_lines: int = 14,
                 max_chars: int = 600) -> str:
    """What the one-click button's tooltip shows: the ping a click would copy, bounded.

    The first ``max_lines`` lines, each cut to ``TOOLTIP_LINE_CHARS``, the whole then cut
    to ``max_chars``. Every cut is marked: a shortened line and a shortened whole end in
    an ellipsis, and lines left out are stood for by one line that is only an ellipsis.
    With ``pending`` (a time is set and this copy will use it up) one more line says so,
    after the cut - it is never lost. An empty ping is its own sentence and resets
    nothing, so it gets no such line.
    """
    text = composed.text if isinstance(composed.text, str) else ""
    if not text:
        return _NOTHING_TO_COPY
    every = text.splitlines()
    lines = [_cut(line, TOOLTIP_LINE_CHARS) for line in every[:max(1, max_lines)]]
    if len(every) > len(lines):
        lines.append(_ELLIPSIS)
    shown = _cut("\n".join(lines), max_chars)
    return shown + "\n" + _WHEN_RESETS if pending else shown
