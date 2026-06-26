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
    Channel link   player:   <url=joinChannel:player_{id}//None//None>{name}</url>
                   built-in: <url=joinChannel:{id}>{name}</url>   (positive id)

The server truncates a MOTD around ~3,000 raw-markup characters, and markup
counts toward that budget, so ``estimate_length`` measures the raw markup length
and ``MOTD_BUDGET_DEFAULT`` is the conservative ceiling callers warn against.
"""
from __future__ import annotations

import html
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

# Player-channel joinChannel links: the MODERN client emits a COMPOUND form,
# ``<url=joinChannel:player_-84651075//None//None>Name</url>``, where the core is
# the channel id (the negative integer from the chat-log header). Built-in
# channels keep a bare positive id. We default to the compound form for player
# channels (high confidence it works today); flip this to ``False`` if a live
# client ever requires the bare ``<url=joinChannel:{id}>`` form for player
# channels too.
CHANNEL_LINK_COMPOUND = True

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


# Delta colour markup for the MOTD ideal-fleet feedback. Positive delta = under
# target (need more) -> red; negative = over target (excess) -> green. EVE colour
# tags are <color=0xAARRGGBB>...</color>. Returns "" for a zero/None delta.
def delta_markup(delta) -> str:
    if not delta:
        return ""
    if delta > 0:
        return f" <color=0xffff4040>+{delta}</color>"
    return f" <color=0xff45d945>{delta}</color>"   # delta already carries the minus sign


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


def channel_text(name: str, channel_id: str | int | None = None) -> str:
    """Render a chat channel name, clickable only when an id is known.

    Chat logs expose a ``Channel ID`` in the log header (see
    ``intel_monitor.read_channel_id``): a NEGATIVE integer for PLAYER channels,
    a small POSITIVE one for built-in channels. With no id the result is plain
    text. With an id the link form depends on the channel kind:

    * Player channel — id string starts with ``-`` or ``player_``. The modern
      client wants the COMPOUND form
      ``<url=joinChannel:player_{core}//None//None>{name}</url>`` where ``core``
      is the id with any leading ``player_`` stripped (so ``-84651075`` →
      ``player_-84651075//None//None``; ``player_-84651075`` is not
      double-prefixed). When :data:`CHANNEL_LINK_COMPOUND` is False, a bare
      ``<url=joinChannel:{id}>{name}</url>`` is emitted instead.
    * Built-in channel — a positive id (digits, no minus) → always bare
      ``<url=joinChannel:{id}>{name}</url>``.

    Examples::

        channel_text("Cap Chain Alpha")                       -> "Cap Chain Alpha"
        channel_text("Cap Chain Alpha", channel_id="-84651075")
        -> "<url=joinChannel:player_-84651075//None//None>Cap Chain Alpha</url>"
        channel_text("Help", channel_id=2)
        -> "<url=joinChannel:2>Help</url>"
    """
    if channel_id is None:
        return name

    id_str = str(channel_id).strip()
    if not id_str:
        return name

    is_player = id_str.startswith("-") or id_str.startswith("player_")
    if is_player:
        # Normalise to the bare core id (strip a leading "player_" if present).
        core = id_str[len("player_"):] if id_str.startswith("player_") else id_str
        if CHANNEL_LINK_COMPOUND:
            return f"<url=joinChannel:player_{core}//None//None>{name}</url>"
        return f"<url=joinChannel:{core}>{name}</url>"

    # Built-in / positive id: always the bare form.
    return f"<url=joinChannel:{id_str}>{name}</url>"


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
    channel_id: str | int | None = None,
    header: str = "",
    footer: str = "",
    staging_name: str | None = None,
    staging_system_id: int | None = None,
    text_color: str | None = "0xffffffff",
    leading_break: bool = True,
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
    * an optional ``Logi: <channel_text>`` line — clickable when ``channel_id``
      is supplied (see :func:`channel_text`), plain text otherwise,
    * optional ``footer`` free text.

    Each fit tuple is ``(dna, name)`` or ``(dna, name, delta)`` where the
    optional ``delta`` is an integer emitted as separate colored text AFTER the
    fitting link (not inside the link name): red when positive (under target),
    green when negative (over target). Empty header/footer and empty tag lists
    are skipped so the result has no dangling blank lines.

    Finally, when ``text_color`` is non-None (default ``"0xffffffff"``, white),
    the FULL assembled body is wrapped in ``<color={text_color}>…</color>``. The
    in-game default text colour renders red/hard-to-read, so white is the sane
    default; pass ``text_color=None`` to emit no wrapper.
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
        parts = []
        for fit in fits:
            dna, name = fit[0], fit[1]
            delta = fit[2] if len(fit) > 2 else 0
            parts.append(fitting_link(dna, name) + delta_markup(delta))
        links = " | ".join(parts)
        lines.append(f"{tag}: {links}")

    if channel:
        lines.append(f"Logi: {channel_text(channel, channel_id)}")

    if footer:
        lines.append(footer)

    body = LINE_BREAK.join(lines)

    # A leading line break (default on) so the first line (FC/header) starts on a
    # fresh line in-game rather than butting against the MOTD field's top edge.
    if leading_break and body:
        body = LINE_BREAK + body

    # Wrap the whole body in a colour tag (white by default — the in-game
    # default renders red/hard-to-read). text_color=None skips the wrapper.
    if text_color is not None:
        return f"<color={text_color}>{body}</color>"
    return body


def estimate_length(motd: str) -> int:
    """Return the raw-markup length of a MOTD (markup counts toward the budget)."""
    return len(motd)


# A MOTD authored or edited in the EVE client and read back via ESI uses the
# client's native anchor form, <a href="target">name</a> (single or double
# quoted, often wrapped in <font ...> runs), rather than the <url=target>name
# </url> form this tool generates. Normalising anchors to the <url=...> form lets
# the extraction regexes below match a client-authored MOTD as well as ours.
_ANCHOR_LINK_RE = re.compile(
    r'<a\s+href=(?P<q>["\'])(?P<target>.*?)(?P=q)\s*>(?P<name>.*?)</a>',
    re.IGNORECASE | re.DOTALL)


def _normalize_links(markup: str) -> str:
    """Rewrite client-native ``<a href="X">name</a>`` anchors to the canonical
    ``<url=X>name</url>`` form so :func:`parse_motd` recognises both."""
    return _ANCHOR_LINK_RE.sub(
        lambda m: f"<url={m.group('target')}>{m.group('name')}</url>", markup)


# EVE link markup is not valid XML, so these are matched with regex, not a parser.
# Non-greedy name capture stops at the first closing tag; the DNA capture is
# everything up to the link's closing ``>``.
_FITTING_RE = re.compile(r"<url=fitting:(?P<dna>[^>]+)>(?P<name>.*?)</url>")
_SHOWINFO_RE = re.compile(r"<url=showinfo:\d+//(?P<cid>\d+)>(?P<name>.*?)</url>")
# Solar-system showinfo links use typeID 5 (vs. 1377 for characters); this only
# matches staging-system links so a character link never reads as a staging one.
_STAGING_RE = re.compile(r"<url=showinfo:5//(?P<sid>\d+)>(?P<name>.*?)</url>")
# joinChannel links: the id token is everything up to the link's closing ``>``
# (kept raw — it may be a bare positive id or a compound
# ``player_-84651075//None//None`` form); the name is the display text.
_CHANNEL_RE = re.compile(r"<url=joinChannel:(?P<id>[^>]+)>(?P<name>.*?)</url>")


def parse_motd(markup: str) -> dict:
    """Extract the FC, staging, channel and fitting links from an existing MOTD.

    Returns a dict ``{"fc", "staging", "channel", "fittings", "raw"}`` where:

    * ``fc`` is ``{"name", "character_id"}`` for the **first** showinfo character
      link, or ``None`` if there is no character link,
    * ``staging`` is ``{"system_id", "name"}`` parsed from the **first**
      ``<url=showinfo:5//{id}>{name}</url>`` link (typeID 5 = Solar System), or
      ``None`` when there is no system link (a character link is typeID 1377 and
      is not matched),
    * ``channel`` is ``{"name", "id"}`` parsed from the **first**
      ``<url=joinChannel:{id}>{name}</url>`` link, where ``id`` is the raw token
      (a bare positive id, or a compound ``player_-…//None//None`` form) and
      ``name`` is the display text, or ``None`` when there is no channel link,
    * ``fittings`` is a list of ``{"dna", "name"}`` for every embedded
      ``<url=fitting:…>`` link, in order of appearance,
    * ``raw`` is the original ``markup`` unchanged.

    Both the ``<url=…>`` form this tool generates and the EVE client's native
    ``<a href="…">name</a>`` anchor form (single or double quoted, possibly
    wrapped in ``<font …>`` runs) are accepted — anchors are normalised to the
    ``<url=…>`` form before extraction. Display names are HTML-entity-decoded
    (e.g. ``&amp;`` → ``&``); link targets/ids are left raw.

    The extracted DNAs can be fed to ``fit_parser.parse_dna`` to offer importing
    those fits into the library.
    """
    normalized = _normalize_links(markup)

    fittings = [
        {"dna": m.group("dna"), "name": html.unescape(m.group("name"))}
        for m in _FITTING_RE.finditer(normalized)
    ]

    fc = None
    char_match = _SHOWINFO_RE.search(normalized)
    if char_match is not None:
        fc = {
            "name": html.unescape(char_match.group("name")),
            "character_id": int(char_match.group("cid")),
        }

    staging = None
    staging_match = _STAGING_RE.search(normalized)
    if staging_match is not None:
        staging = {
            "system_id": int(staging_match.group("sid")),
            "name": html.unescape(staging_match.group("name")),
        }

    channel = None
    channel_match = _CHANNEL_RE.search(normalized)
    if channel_match is not None:
        channel = {
            "name": html.unescape(channel_match.group("name")),
            "id": channel_match.group("id"),
        }

    return {
        "fc": fc,
        "staging": staging,
        "channel": channel,
        "fittings": fittings,
        "raw": markup,
    }
