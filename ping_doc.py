"""Pure, Tk-free template model for the Discord ping.

The ping reuses the MOTD composer's run dataclasses -- :class:`motd_doc.TextRun`
(free text) and :class:`motd_doc.TokenRun` (a pill) -- with six pill kinds of
its own, none of which takes a parameter. This module owns those kinds, the
basic template, and the walk that turns a template plus already-resolved values
into the plain text that goes on the clipboard. It has no Tkinter, no network
and no config access, and imports only :mod:`motd_doc`, so it is safe to call
from any thread.

How a template becomes text (:func:`resolve`):

    1. Runs are emitted in order. Free text is emitted VERBATIM -- no escaping
       (Discord markdown is the user's to type) and any styling stored on a
       ``TextRun`` is ignored.
    2. Lines are split on ``\\n`` inside text runs. A line that holds at least
       one pill, ALL of whose pills are empty, is dropped whole -- its label
       text and its newline included -- with one warning per dropped kind.
    3. A line with some empty and some filled pills keeps the filled ones; the
       empty ones add nothing and do not warn.
    4. A pill of a kind this module does not know (a MOTD pill pasted in) adds
       nothing, warns once per kind, and counts as empty for rule 2.
    5. Trailing newlines are removed. An empty document yields ``""``.

Every value arrives as its final display string (``PingValues.when`` is
formatted by the caller); ``""`` means "nothing to show".
"""
from __future__ import annotations

from dataclasses import dataclass

from motd_doc import TextRun, TokenLabel, TokenRun, _ellip, doc_from_json

__all__ = ["PING_KINDS", "FIELD_LABELS", "PingValues", "PingText",
           "default_doc", "resolve", "chip_label", "clean_doc"]

PING_KINDS = ("fleet_type", "fc", "location", "when", "doctrine", "comms")

FIELD_LABELS = {"fleet_type": "Fleet type", "fc": "FC", "location": "Location",
                "when": "When", "doctrine": "Doctrine", "comms": "Comms"}

_DASH = chr(0x2014)          # em dash, built at runtime: the source stays pure ASCII
_CHIP_LIMIT = 40


@dataclass(frozen=True)
class PingValues:
    """The six values a ping is filled with -- each the final display string.

    ``""`` means there is nothing to show for that field.
    """

    fleet_type: str = ""
    fc: str = ""
    location: str = ""
    when: str = ""
    doctrine: str = ""
    comms: str = ""


@dataclass
class PingText:
    text: str
    warnings: list           # human sentences, in document order, no duplicates
    omitted: list            # kinds whose line was dropped, in document order, no duplicates


# --- the basic template ---------------------------------------------------

def default_doc() -> list:
    """The basic template as runs: the mention, a blank line, then one line per field.

    Built fresh on every call -- the runs are mutable and callers edit them.
    """
    return [
        TextRun("@everyone\n\n"),
        TokenRun("fleet_type"),
        TextRun("\nFC: "),
        TokenRun("fc"),
        TextRun("\nLocation: "),
        TokenRun("location"),
        TextRun("\nWhen: "),
        TokenRun("when"),
        TextRun("\nDoctrine: "),
        TokenRun("doctrine"),
        TextRun("\nComms: "),
        TokenRun("comms"),
    ]


# --- template -> text -----------------------------------------------------

def _is_ping_kind(kind) -> bool:
    return isinstance(kind, str) and kind in PING_KINDS


def _value(kind, values) -> str:
    """The display string for ``kind``; ``""`` for an unknown kind or no value."""
    if not _is_ping_kind(kind):
        return ""
    value = getattr(values, kind, "")
    if value is None:
        return ""
    return value if isinstance(value, str) else str(value)


def _plain(text) -> str:
    """A text run's text as a string (a hand-edited template may store ``null`` / a number)."""
    if text is None:
        return ""
    return text if isinstance(text, str) else str(text)


def _lines(doc) -> list:
    """Split a run list into lines; each line is a list of ``(is_pill, payload)`` parts.

    ``payload`` is the text for a text part and the kind for a pill. Only the
    ``\\n`` inside text runs ends a line, so there is always at least one line.
    """
    lines: list = [[]]
    for run in doc or ():
        if isinstance(run, TokenRun):
            lines[-1].append((True, run.kind))
        elif isinstance(run, TextRun):
            first, *rest = _plain(run.text).split("\n")
            if first:
                lines[-1].append((False, first))
            for segment in rest:
                lines.append([(False, segment)] if segment else [])
    return lines


def _add_once(items: list, item) -> None:
    if item not in items:
        items.append(item)


def resolve(doc: list, values: PingValues) -> PingText:
    """Turn a template and its values into the ping text (rules in the module docstring)."""
    kept: list = []
    warnings: list = []
    omitted: list = []
    for parts in _lines(doc):
        kinds = [payload for is_pill, payload in parts if is_pill]
        pieces = [_value(payload, values) if is_pill else payload
                  for is_pill, payload in parts]
        dropped = bool(kinds) and not any(_value(kind, values) for kind in kinds)
        for kind in kinds:
            if not _is_ping_kind(kind):
                _add_once(warnings, f"Unsupported pill ignored: {kind}")
            elif dropped:
                _add_once(warnings,
                          f"{FIELD_LABELS[kind]} is empty {_DASH} its line was left out")
                _add_once(omitted, kind)
        if not dropped:
            kept.append("".join(pieces))
    return PingText("\n".join(kept).rstrip("\n"), warnings, omitted)


# --- chip labels ----------------------------------------------------------

def chip_label(kind: str, values: PingValues) -> TokenLabel:
    """The chip a pill shows: its current value, or the field name (stale) when empty."""
    if not _is_ping_kind(kind):
        return TokenLabel(_ellip(kind, _CHIP_LIMIT), False,
                          f"Unsupported pill ignored: {kind}")
    label = FIELD_LABELS[kind]
    value = _value(kind, values)
    if not value:
        return TokenLabel(label, False,
                          f"{label} is empty {_DASH} its line will be left out")
    return TokenLabel(_ellip(value, _CHIP_LIMIT), True,
                      f"{label} {_DASH} change it under THIS PING")


# --- stored JSON -> runs --------------------------------------------------

def clean_doc(data) -> list:
    """Tolerant loader for a stored template: always returns RUNS, never JSON.

    Anything that is not a list gives :func:`default_doc`. A list is loaded
    through :func:`motd_doc.doc_from_json` item by item: only dict items whose
    ``t`` is ``"text"`` or ``"token"`` are read, and an item the loader cannot
    read (hand-edited ``params`` / ``style`` of the wrong type) is skipped
    rather than raised on. ``[]`` stays ``[]`` -- an intentionally empty
    template. Unknown pill kinds are kept verbatim; :func:`resolve` reports them.
    """
    if not isinstance(data, list):
        return default_doc()
    runs: list = []
    for item in data:
        if not isinstance(item, dict) or item.get("t") not in ("text", "token"):
            continue
        try:
            runs.extend(doc_from_json([item]))
        except (AttributeError, TypeError, ValueError):
            continue
    return runs
