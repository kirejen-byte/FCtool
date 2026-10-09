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
    2. Lines are split on ``\\n`` inside text runs (``\\r\\n`` and a bare
       ``\\r`` count as ``\\n``). A line that holds at least
       one pill, ALL of whose pills are empty, is dropped whole -- its label
       text and its newline included -- with one warning per dropped kind.
    3. A line with some empty and some filled pills keeps the filled ones; the
       empty ones add nothing and do not warn.
    4. A pill of a kind this module does not know (a MOTD pill pasted in) adds
       nothing, warns once per kind, and counts as empty for rule 2.
    5. Trailing newlines are removed. An empty document yields ``""``.

Every value arrives as its final display string (``PingValues.when`` is
formatted by the caller); ``""`` means "nothing to show", and so does any value
that is not a string.
"""
from __future__ import annotations

from dataclasses import dataclass

from motd_doc import (TextRun, TokenLabel, TokenRun, _ellip, _style_color, _style_size,
                      doc_from_json)

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
    """The display string for ``kind``; ``""`` for an unknown kind or no value.

    A value that is not a string counts as EMPTY: a wiring mistake (an
    ``(id, name)`` tuple, ``False``) must never be printed into a ping.
    """
    if not _is_ping_kind(kind):
        return ""
    value = getattr(values, kind, "")
    return value if isinstance(value, str) else ""


def _plain(text) -> str:
    """Text as a string with ``\\n`` line ends -- the ONE text normaliser.

    ``None`` is ``""`` and any other non-string its ``str()`` (a hand-edited
    template may store ``null`` / a number); ``\\r\\n`` and a bare ``\\r``
    become ``\\n``. :func:`clean_doc` applies it to stored text; :func:`resolve`
    applies it too, so runs built by hand cannot raise or hide a line end.
    """
    if text is None:
        return ""
    if not isinstance(text, str):
        text = str(text)
    return text.replace("\r\n", "\n").replace("\r", "\n")


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
        pieces: list = []
        kinds: list = []
        filled = False
        for is_pill, payload in parts:
            if is_pill:
                value = _value(payload, values)      # looked up once per pill
                kinds.append(payload)
                filled = filled or bool(value)
                pieces.append(value)
            else:
                pieces.append(payload)
        dropped = bool(kinds) and not filled
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

def _clean_style(style) -> dict:
    """The usable part of a stored text style; junk costs the style, never the text."""
    if not isinstance(style, dict):
        return {}
    out: dict = {}
    color = _style_color(style.get("color"))
    if color is not None:
        out["color"] = color
    for key in ("bold", "italic", "underline"):
        if style.get(key) is True:
            out[key] = True
    size = _style_size(style.get("size"))
    if size is not None:
        out["size"] = size
    return out


def _clean_item(item) -> "dict | None":
    """One stored item in a form :func:`motd_doc.doc_from_json` cannot choke on, or None."""
    if not isinstance(item, dict):
        return None
    form = item.get("t")
    if form == "text":
        return {"t": "text", "text": _plain(item.get("text")),
                "style": _clean_style(item.get("style"))}
    if form == "token":
        kind = item.get("kind")
        if not isinstance(kind, str) or not kind:
            return None
        params = item.get("params")
        return {"t": "token", "kind": kind,
                "params": params if isinstance(params, dict) else {}}
    return None


def clean_doc(data) -> list:
    """Tolerant loader for a stored template: always returns RUNS, never JSON.

    Anything that is not a list gives :func:`default_doc`. A list is loaded
    through :func:`motd_doc.doc_from_json`, over its dict items whose ``t`` is
    ``"text"`` or ``"token"`` (anything else is skipped); ``[]`` stays ``[]`` --
    an intentionally empty template. Its output goes to a Tk Text widget and
    back into the config, so every item is NORMALISED on the way through:

    * a text item's ``text`` is always a ``str`` with ``\\n`` line ends
      (:func:`_plain`); a style that is not a dict, or a junk value inside one,
      costs that styling only -- the text is never lost;
    * a token item with a missing, empty or non-string ``kind`` is dropped;
      ``params`` that are not a dict become ``{}`` and the pill is KEPT (a pill
      that vanished would leave its label behind with no warning).

    Unknown pill kinds are kept verbatim; :func:`resolve` reports them.
    """
    if not isinstance(data, list):
        return default_doc()
    items = [clean for clean in map(_clean_item, data) if clean is not None]
    return doc_from_json(items)
