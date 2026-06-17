"""Pure, Tk-free assembly and parsing of EVE fleet MOTD markup.

A fleet MOTD is rich text using EVE's in-game link markup (which is *not* valid
XML — it is parsed and generated here with plain string ops and regex, never an
XML parser). This module composes a MOTD from a doctrine's fits and parses an
existing MOTD back into structured data, with no Tkinter, no network, and no
dependency on other project modules. It works purely on strings and dicts and is
safe to call from any thread.

Markup reference (verified 2026-06-16, see
docs/superpowers/specs/2026-06-16-fitting-doctrine-motd-design.md §3.4/§3.5):

    Fitting link   <url=fitting:{DNA}>{name}</url>
    Character link <url=showinfo:{type_id}//{character_id}>{name}</url>
    Channel link   <url=joinChannel:{channel_id}>{name}</url>   (numeric id only)

The server truncates a MOTD around ~3,000 raw-markup characters, and markup
counts toward that budget, so ``estimate_length`` measures the raw markup length
and ``MOTD_BUDGET_DEFAULT`` is the conservative ceiling callers warn against.
"""
from __future__ import annotations

import re

# Generic "character" group type id used in showinfo links. 1377 (or 1) resolves
# to a clickable character link in-game; isolated here so it is trivial to change
# if 1377 ever stops resolving.
CHAR_SHOWINFO_TYPE_ID = 1377

# Conservative raw-markup character budget for a fleet MOTD. ESI declares no
# maxLength, but the server truncates around ~3,000 raw chars; warn well before.
MOTD_BUDGET_DEFAULT = 3000

# Line separator in MOTD markup (not a newline — EVE uses the <br> tag).
LINE_BREAK = "<br>"

# Tags are emitted in this stable priority; any tag not listed follows in the
# caller's iteration order. Keeps DPS/Logistics/Links at the top regardless of
# how ``fits_by_tag`` happens to be ordered.
TAG_PRIORITY = ("DPS", "Logistics", "Links")


def fitting_link(dna: str, name: str) -> str:
    """Build a self-contained in-game fitting link from a fit's DNA.

    The DNA fully encodes the fit, so the client rebuilds it on click without
    any ESI write. Example::

        fitting_link("12015:2185;5::", "Arty Muninn")
        -> "<url=fitting:12015:2185;5::>Arty Muninn</url>"
    """
    return f"<url=fitting:{dna}>{name}</url>"


def char_link(character_id: int, name: str, type_id: int = CHAR_SHOWINFO_TYPE_ID) -> str:
    """Build a showinfo link to a character.

    Uses the generic character group ``type_id`` (default
    :data:`CHAR_SHOWINFO_TYPE_ID`)::

        char_link(90000001, "Securitas Protector")
        -> "<url=showinfo:1377//90000001>Securitas Protector</url>"
    """
    return f"<url=showinfo:{type_id}//{character_id}>{name}</url>"


def system_link(system_id: int, name: str) -> str:
    """Build a showinfo link to a solar system.

    Type id ``5`` is EVE's Solar System typeID, so the client opens the system
    info window on click::

        system_link(30000142, "Jita")
        -> "<url=showinfo:5//30000142>Jita</url>"
    """
    return f"<url=showinfo:5//{system_id}>{name}</url>"


def channel_text(name: str, channel_id: int | None = None) -> str:
    """Render a chat channel name, clickable only when a numeric id is known.

    Chat logs do not expose channel ids, so the default is plain text. A numeric
    ``channel_id`` (negative for player channels) yields a joinChannel link::

        channel_text("Cap Chain Alpha")               -> "Cap Chain Alpha"
        channel_text("Cap Chain Alpha", channel_id=-99)
        -> "<url=joinChannel:-99>Cap Chain Alpha</url>"
    """
    if channel_id is None:
        return name
    return f"<url=joinChannel:{channel_id}>{name}</url>"


def _ordered_tags(tags: list[str]) -> list[str]:
    """Return ``tags`` with :data:`TAG_PRIORITY` first, then the rest in order."""
    priority = [t for t in TAG_PRIORITY if t in tags]
    rest = [t for t in tags if t not in TAG_PRIORITY]
    return priority + rest


def build_motd(
    *,
    fc_name: str | None,
    fc_character_id: int | None,
    doctrine_name: str,
    fits_by_tag: dict[str, list[tuple[str, str]]],
    channel: str | None = None,
    header: str = "",
    footer: str = "",
    staging_name: str | None = None,
    staging_system_id: int | None = None,
) -> str:
    """Compose a fleet MOTD from a doctrine's fits.

    Lines are joined by ``<br>``:

    * optional ``header`` free text,
    * an FC line ``FC: <char_link>`` — omitted entirely when ``fc_name`` is None,
    * an optional ``Staging: <system_link>`` line, emitted only when BOTH
      ``staging_name`` and ``staging_system_id`` are provided (sits right after
      the FC line and before the Doctrine line),
    * ``Doctrine: <name>`` with the name as plain text (no markup),
    * one labelled line per tag (``<Tag>: <fitting_link> | <fitting_link> …``),
      ordered by :data:`TAG_PRIORITY` then the caller's order, each ``(dna, name)``
      rendered via :func:`fitting_link`,
    * an optional ``Logi: <channel_text>`` line,
    * optional ``footer`` free text.

    Each fit tuple is ``(dna, name)``. Empty header/footer and empty tag lists are
    skipped so the result has no dangling blank lines.
    """
    lines: list[str] = []

    if header:
        lines.append(header)

    if fc_name is not None and fc_character_id is not None:
        lines.append(f"FC: {char_link(fc_character_id, fc_name)}")

    if staging_name is not None and staging_system_id is not None:
        lines.append(f"Staging: {system_link(staging_system_id, staging_name)}")

    lines.append(f"Doctrine: {doctrine_name}")

    for tag in _ordered_tags(list(fits_by_tag.keys())):
        fits = fits_by_tag.get(tag) or []
        if not fits:
            continue
        links = " | ".join(fitting_link(dna, name) for dna, name in fits)
        lines.append(f"{tag}: {links}")

    if channel:
        lines.append(f"Logi: {channel_text(channel)}")

    if footer:
        lines.append(footer)

    return LINE_BREAK.join(lines)


def estimate_length(motd: str) -> int:
    """Return the raw-markup length of a MOTD (markup counts toward the budget)."""
    return len(motd)


# EVE link markup is not valid XML, so these are matched with regex, not a parser.
# Non-greedy name capture stops at the first closing tag; the DNA capture is
# everything up to the link's closing ``>``.
_FITTING_RE = re.compile(r"<url=fitting:(?P<dna>[^>]+)>(?P<name>.*?)</url>")
_SHOWINFO_RE = re.compile(r"<url=showinfo:\d+//(?P<cid>\d+)>(?P<name>.*?)</url>")


def parse_motd(markup: str) -> dict:
    """Extract the FC and embedded fitting links from an existing MOTD.

    Returns a dict ``{"fc", "fittings", "raw"}`` where:

    * ``fc`` is ``{"name", "character_id"}`` for the **first** showinfo character
      link, or ``None`` if there is no character link,
    * ``fittings`` is a list of ``{"dna", "name"}`` for every embedded
      ``<url=fitting:…>`` link, in order of appearance,
    * ``raw`` is the original ``markup`` unchanged.

    The extracted DNAs can be fed to ``fit_parser.parse_dna`` to offer importing
    those fits into the library.
    """
    fittings = [
        {"dna": m.group("dna"), "name": m.group("name")}
        for m in _FITTING_RE.finditer(markup)
    ]

    fc = None
    char_match = _SHOWINFO_RE.search(markup)
    if char_match is not None:
        fc = {
            "name": char_match.group("name"),
            "character_id": int(char_match.group("cid")),
        }

    return {"fc": fc, "fittings": fittings, "raw": markup}
