"""Pure, Tk-free document model for the fleet MOTD pill composer.

The MOTD sub-tab is a free-form rich-text canvas whose *pills* (live tokens)
re-resolve on every push. This module is the model behind that canvas: an ordered
list of runs — styled :class:`TextRun` text and :class:`TokenRun` live tokens —
plus the pure functions that turn a run list into EVE MOTD markup, migrate the
v1 template fields, and parse an imported MOTD back into runs. It has no Tkinter,
no network, and depends only on :mod:`motd_builder` (link builders + ``_escape``
+ ``LINE_BREAK``) and :mod:`motd_markup` (styled-segment parse/serialise), so it
is safe to call from any thread.

Escaping contract (mirrors the MOTD field's rules — see
docs/superpowers/specs/2026-07-21-motd-composer-redesign-design.md §4):

    The model stores PLAIN, unescaped text. ALL escaping (``&`` first, then
    ``<``/``>`` via :func:`motd_builder._escape`) happens at serialisation — for
    free text AND escaped labels (tag / doctrine names) alike. Correspondingly
    every path that parses markup INTO the model (legacy header/footer migration,
    imported MOTDs) runs ``html.unescape`` on text so a round-trip never
    double-escapes. Fit / character / system / channel *names* are link LABELS
    and are escaped the same way, by the :mod:`motd_builder` link builders
    (``fitting_link`` / ``char_link`` / ``system_link`` / ``channel_text``):
    a name is data, and written raw it could close its own link and open
    another. Those builders also VALIDATE the link target (fit DNA, character /
    system id, channel id): a pill whose target is not one renders as its
    escaped plain label with no link, and is UNRESOLVED -- one warning line,
    counted, and its chip (:func:`token_label`) says so. (A ``channel`` pill
    with no id is plain text by design and is not.) A text run's colour and
    size are written inside a tag too, so only ``#rrggbb`` and an int size in
    1..999 are handed to the writer. :func:`_runs_from_markup` unescapes a
    link's label exactly as it unescapes free text, so render, parse, render
    is a fixed point and a pill gets its ORIGINAL name back.

Resolution walk: runs are emitted in order. Free text is escaped then wrapped in
its style via :func:`motd_markup.segments_to_markup`; ``\n`` becomes ``<br>``.
Tokens resolve per the §4.2 table (see :func:`_resolve_token`). A *line/block*
token that resolves empty is omitted and swallows one adjacent newline so an
omitted line leaves no blank line — implemented by deferring newline emission
(pending breaks flush only before real content, and a trailing pending break is
dropped), which also keeps the whole thing byte-identical to ``build_motd``. The
non-empty body then gets the same envelope as ``build_motd``: a leading
``<br>`` and a whole-body ``<color=0xffffffff>…</color>`` wrap.
"""
from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from typing import Callable

from motd_builder import (
    LINE_BREAK,
    _escape,
    _ordered_tags,
    channel_text,
    char_link,
    delta_markup,
    fitting_link,
    is_valid_dna,
    is_valid_id,
    parse_id,
    system_link,
)
from motd_markup import Segment, parse_markup, segments_to_markup


# --- runs -----------------------------------------------------------------

@dataclass
class TextRun:
    """A styled run of plain text (mirrors :class:`motd_markup.Segment` styles).

    ``text`` may contain ``\\n`` (serialised as ``<br>``); it is stored plain and
    escaped only at :func:`resolve` time. ``color`` is a ``"#rrggbb"`` string.
    """

    text: str
    color: str | None = None
    bold: bool = False
    italic: bool = False
    underline: bool = False
    size: int | None = None


@dataclass
class TokenRun:
    """A live token — a JSON-serialisable spec resolved to markup at compose time."""

    kind: str
    params: dict = field(default_factory=dict)


Doc = list  # list[TextRun | TokenRun]


# --- resolution context + results ----------------------------------------

@dataclass
class ResolveContext:
    """Everything :func:`resolve` needs, injected by the wiring (all Tk-free)."""

    selected_doctrine: object | None = None                       # has .name; None = none
    fits_by_tag: Callable[[bool], dict] = lambda compact: {}       # compact -> {tag: [(dna, name)]}
    deltas: dict = field(default_factory=dict)                    # ship_type_id -> int
    canonical_dna: Callable[[str, object], str] = lambda dna, parsed=None: dna
    parse_fit: Callable[[str], object] = lambda dna: None          # -> obj w/ .ship_type_id, or None
    resolve_system: Callable[[str], "int | None"] = lambda name: None
    resolve_channel_id: Callable[[str], object] = lambda name: None  # str | int | None
    fc_selected: tuple | None = None                             # (character_id, name) or None
    legacy_fits: list | None = None                             # [(dna, name)] template fallback
    # Doctrine refits (design 2026-09-12 §13): given a fit pill's DNA, return the
    # ``(dna, name)`` of that slot's ACTIVE refit when the DNA belongs to a slot
    # WITH refits in the selected doctrine AND the active fit differs; else None.
    # Every fit link in the document is substituted through it, so a saved
    # template's hand-placed pill FOLLOWS the FC's refit swap without the stored
    # document ever being mutated (templates stay literal on disk).
    refit_active: Callable[[str], "tuple | None"] = lambda dna: None


@dataclass
class TokenLabel:
    """A chip's display data: a short markup-free label, live/stale, plus tooltip."""

    text: str            # chip label, <=40 chars (ellipsised), markup-free
    resolved: bool
    tooltip: str
    delta: int = 0       # net delta for fit / tag_line chips (0 = hide)


@dataclass
class ResolvedMotd:
    markup: str
    warnings: list
    unresolved: int


TOKEN_KINDS = ("char", "fit", "system", "channel", "fc_line", "staging_line",
               "doctrine_line", "tag_line", "channel_line", "doctrine_block")

# Line/block tokens: when they resolve empty they omit a *line* and swallow one
# adjacent newline. Inline item tokens (char/fit/system/channel) never do.
_LINE_KINDS = frozenset({"fc_line", "staging_line", "doctrine_line", "tag_line",
                         "channel_line", "doctrine_block"})

# Warning strings. The tag_line and staging_line forms are byte-parity with
# today's ``_current_motd_markup``; the rest follow the same shape.
_FC_WARN = "FC pill unresolved — its line was omitted"
_DOCTRINE_WARN = "Doctrine pill unresolved — its line was omitted"

# Owner amendment 2026-07-22 (spec §4.2): a channel_line's default label is
# "Channel" for every NEW creation (Lines & blocks palette item, fresh-insert
# channel prompt, default_doc) — "Logi:" visually collided with the "Logi:"
# fits tag_line rendered just above it. EXCEPTION: ``from_legacy_fields`` KEEPS
# emitting the literal "Logi" so v1 templates render byte-identically to what
# they always produced; it does not read this constant (see its docstring).
CHANNEL_LINE_DEFAULT_LABEL = "Channel"

# The tag_lines a fresh document falls back to when there is nothing better to go
# on (no doctrine selected, or a doctrine with no tagged members). Deliberately
# the pre-2026-07-26 hardcoded triple, so a fresh install's default MOTD is
# unchanged; a doctrine that DOES carry role tags supplies its own set instead
# (see :func:`doctrine_tag_lines` and ``default_doc``'s ``default_tags``).
DEFAULT_TAG_LINES = ("DPS", "Logi", "Links")


# --- fit finalisation (canonical DNA + delta lookup) ----------------------

def _refit_substitute(dna: str, name: str, ctx: ResolveContext):
    """``(dna, name)`` after the doctrine-refit substitution (design §13).

    THE single substitution point: every fit link in the document is emitted by
    :func:`_finalize_fit`, so routing it here makes "pills follow the active
    refit" uniform across the ``fit`` token, ``tag_line``, ``doctrine_block`` and
    the v1 ``legacy_fits`` fallback.

    Deliberately total: an out-of-contract ``refit_active`` (raising, returning a
    bare string, a 1-tuple, an empty dna) leaves the pair UNCHANGED rather than
    corrupting a link — the worst case for a broken provider is today's
    behaviour. A substitute whose name is blank keeps the original link text.
    """
    try:
        sub = ctx.refit_active(dna)
    except Exception:
        return dna, name
    if not isinstance(sub, (tuple, list)) or len(sub) != 2:
        return dna, name
    sub_dna, sub_name = sub
    if not isinstance(sub_dna, str) or not sub_dna:
        return dna, name
    return sub_dna, (sub_name if isinstance(sub_name, str) and sub_name else name)


def _finalize_fit(dna: str, name: str, ctx: ResolveContext):
    """Return ``(markup, delta, parsed_ok)`` for one fit, matching ``build_motd``.

    Applies the refit substitution (:func:`_refit_substitute`), canonicalises the
    DNA, looks up the ideal-fleet delta by the parsed ship type, and emits
    ``fitting_link(canon, name) + delta_markup(delta)``. An unparseable DNA keeps
    the raw DNA and drops the delta (today's behavior).

    ``fitting_link`` escapes the label and refuses a target that is not fit DNA
    (a stored ``fit.dna`` reaches here verbatim when ``to_dna`` raises), in
    which case the result is the escaped label alone. A ``dna`` that is not even
    a string (a hand-edited template) never reaches the injected providers.
    """
    if not isinstance(dna, str):
        return fitting_link(dna, name), 0, False
    dna, name = _refit_substitute(dna, name, ctx)
    parsed = ctx.parse_fit(dna)
    canon = ctx.canonical_dna(dna, parsed)
    delta = ctx.deltas.get(parsed.ship_type_id, 0) if parsed is not None else 0
    return fitting_link(canon, name) + delta_markup(delta), delta, parsed is not None


# --- a pill that renders WITHOUT its link ---------------------------------
#
# The link builders refuse a target that is not one (a character pill with no
# id, a fit whose DNA is not fit DNA, ...) and hand back the escaped label
# alone. That pill is UNRESOLVED: markup, warnings and chip must agree --
# link written <=> no warning <=> chip resolved. ``_resolve_token`` reports it
# here; ``token_label`` mirrors the same target tests for the chip.

def _linked(markup: str) -> bool:
    """True when a link builder wrote its link.

    Reliable because labels are escaped: a refused target's result is the
    escaped label (or ``""``), which can never start with the open tag.
    """
    return markup.startswith("<url=")


def _no_link_warning(what: str, name, why: str) -> str:
    """The ONE warning line for a pill shown as plain text (name clipped --
    every warning shares a single Tk label)."""
    return f"{what} '{_ellip(name)}' {why} — shown as plain text"


def _fit_markup(dna, name, ctx: ResolveContext):
    """``(markup, warnings)`` for one fit link -- :func:`_finalize_fit` plus the
    no-link report. The warning names the label the MOTD actually shows (the
    ACTIVE refit's when the pill follows one)."""
    markup = _finalize_fit(dna, name, ctx)[0]
    if _linked(markup):
        return markup, []
    if isinstance(dna, str):
        name = _refit_substitute(dna, name, ctx)[1]
    return markup, [_no_link_warning("Fit", name, "is not a fit link")]


def _join_fits(fits, ctx: ResolveContext):
    """``(markup, warnings)`` for a line of fits: the links joined by `` | ``
    and one warning per fit that rendered without its link."""
    parts: list = []
    warns: list = []
    for dna, name in fits:
        markup, w = _fit_markup(dna, name, ctx)
        parts.append(markup)
        warns.extend(w)
    return " | ".join(parts), warns


# --- token resolution -----------------------------------------------------

def _resolve_token(run: TokenRun, ctx: ResolveContext, compact: bool,
                   fallback_active: bool, fallback_state: list):
    """Resolve one token to ``(markup, warnings, unresolved)``.

    ``markup`` is ``""`` when the token contributes nothing (omitted line token or
    a silently-degraded inline token). The caller decides layout: for a *line*
    kind an empty ``markup`` swallows a following newline.
    """
    kind, p = run.kind, run.params

    if kind == "char":
        name = p.get("name", "")
        markup = char_link(p.get("id"), name)
        if not _linked(markup):
            return markup, [_no_link_warning("Character", name,
                                             "has no character id")], 1
        return markup, [], 0

    if kind == "fit":
        # A DNA-SHAPED fit always resolves: an unparseable DNA keeps the raw DNA
        # and drops the delta (§4.2 "DNA unparseable → raw DNA kept, no delta";
        # §9 "degrades to … raw DNA"). It is never counted unresolved / rendered
        # stale merely because the type-catalog could not parse it. A target
        # that is not fit DNA at all is refused by ``fitting_link``: the pill
        # renders as its escaped label with NO link, and that IS unresolved --
        # one warning, counted (``_fit_markup``).
        markup, warns = _fit_markup(p.get("dna", ""), p.get("name", ""), ctx)
        return markup, warns, len(warns)

    if kind == "system":
        name = p.get("name", "")
        sid = ctx.resolve_system(name)
        if sid is not None:
            markup = system_link(sid, name)
            if not _linked(markup):     # the resolver handed back a non-id
                return markup, [_no_link_warning("System", name,
                                                 "has no system id")], 1
            return markup, [], 0
        return _escape(name), [], 1  # degrade to plain text, stale

    if kind == "channel":
        name = p.get("name", "")
        if not name:
            return "", [], 0  # silent inline degrade
        return channel_text(name, ctx.resolve_channel_id(name)), [], 0

    if kind == "fc_line":
        if p.get("source") == "pinned":
            cid, name = p.get("id"), p.get("name")
        else:
            cid, name = ctx.fc_selected if ctx.fc_selected else (None, None)
        if cid is not None and name:
            link = char_link(cid, name)
            if not _linked(link):
                return f"FC: {link}", [_no_link_warning("FC", name,
                                                        "has no character id")], 1
            return f"FC: {link}", [], 0
        return "", [_FC_WARN], 1

    if kind == "staging_line":
        name = p.get("name", "")
        sid = ctx.resolve_system(name)
        if sid is not None:
            link = system_link(sid, name)
            if not _linked(link):       # the resolver handed back a non-id
                return f"Staging: {link}", [_no_link_warning(
                    "Staging system", name, "has no system id")], 1
            return f"Staging: {link}", [], 0
        return "", [f"Staging system '{name}' did not resolve to a known system — "
                    "the staging line was omitted."], 1

    if kind == "doctrine_line":
        d = ctx.selected_doctrine
        if d is not None:
            return f"Doctrine: {_escape(d.name)}", [], 0
        return "", [_DOCTRINE_WARN], 1

    if kind == "tag_line":
        tag = p.get("tag", "")
        fits = (ctx.fits_by_tag(compact) or {}).get(tag) or []
        if fits:
            joined, warns = _join_fits(fits, ctx)
            return f"{_escape(tag)}: {joined}", warns, len(warns)
        # empty tag: fall back to the legacy fits at the FIRST empty tag_line.
        if fallback_active and not fallback_state[0]:
            fallback_state[0] = True
            joined, warns = _join_fits(ctx.legacy_fits or [], ctx)
            return f"Fits: {joined}", warns, len(warns)
        if fallback_active:
            return "", [], 0  # remaining empty tag_lines: silent (fits already shown)
        return "", [f"{tag} pill unresolved — its line was omitted"], 1

    if kind == "channel_line":
        name = p.get("name", "")
        if not name:
            return "", [], 0  # omit silently (matches today's ``if channel:`` guard)
        label = p.get("label", CHANNEL_LINE_DEFAULT_LABEL)
        return f"{_escape(label)}: {channel_text(name, ctx.resolve_channel_id(name))}", [], 0

    if kind == "doctrine_block":
        d = ctx.selected_doctrine
        if d is None:
            return "", [_DOCTRINE_WARN], 1
        fbt = ctx.fits_by_tag(compact) or {}
        lines = [f"Doctrine: {_escape(d.name)}"]
        warns: list = []
        for tag in _ordered_tags(list(fbt.keys())):
            fits = fbt.get(tag) or []
            if fits:
                joined, tag_warns = _join_fits(fits, ctx)
                warns.extend(tag_warns)
                lines.append(f"{_escape(tag)}: {joined}")
        return LINE_BREAK.join(lines), warns, len(warns)

    # Unknown kind (saved by a newer version) → skip with a warning.
    return "", [f"Unknown token kind '{kind}' — skipped"], 1


def _has_tag_fit_content(doc: Doc, ctx: ResolveContext, compact: bool) -> bool:
    """True when any tag_line / doctrine_block in ``doc`` would show a fit."""
    fbt = None
    for run in doc:
        if not isinstance(run, TokenRun):
            continue
        if run.kind == "tag_line":
            if fbt is None:
                fbt = ctx.fits_by_tag(compact) or {}
            if fbt.get(run.params.get("tag")):
                return True
        elif run.kind == "doctrine_block" and ctx.selected_doctrine is not None:
            if fbt is None:
                fbt = ctx.fits_by_tag(compact) or {}
            if any(fbt.get(t) for t in fbt):
                return True
    return False


# --- text-run style values --------------------------------------------------
#
# The markup writer puts a run's colour and size INSIDE a tag
# (``<color=0xff..>`` / ``<fontsize=..>``), so they are link-grade data: a saved
# or shared template holding ``"size": "14><url=joinChannel:...>JOIN</url>"``
# would push a working link. Only a real colour / a real size is handed on;
# anything else is dropped for that run (its text still renders, unstyled).
# What the importer can hand over: a colour is always ``#rrggbb`` (lowercase);
# a size is an int from 0 up to nine digits (``motd_markup`` accepts
# ``[0-9]{1,9}``). The renderer keeps sizes 1..999 and drops the rest -- so an
# imported ``<fontsize=0>`` or ``<fontsize=5000>`` run re-renders unsized.

_STYLE_COLOR_RE = re.compile(r"#[0-9a-fA-F]{6}")
_STYLE_SIZE_MAX = 999


def _style_color(color) -> "str | None":
    """``color`` when it is exactly ``#rrggbb`` (ASCII hex), else None."""
    if isinstance(color, str) and _STYLE_COLOR_RE.fullmatch(color) is not None:
        return color
    return None


def _style_size(size) -> "int | None":
    """``size`` when it is an ``int`` (never a ``bool``) in 1..999, else None."""
    if isinstance(size, int) and not isinstance(size, bool) \
            and 1 <= size <= _STYLE_SIZE_MAX:
        return size
    return None


# --- resolution walk ------------------------------------------------------

def resolve(doc: Doc, ctx: ResolveContext, compact: bool = False) -> ResolvedMotd:
    """Serialise ``doc`` to MOTD markup, resolving every token against ``ctx``.

    Free text is escaped and style-wrapped; tokens resolve per §4.2. Omitted
    line/block tokens leave no blank line and never a dangling break. The
    non-empty body is wrapped exactly like :func:`motd_builder.build_motd`
    (leading ``<br>`` + white colour). Returns the markup, EVERY warning line,
    and ``unresolved``: one per omitted line/block token, one per stale inline
    token, one per pill rendered without its link -- and for a line of fits
    (``tag_line``, the legacy ``Fits:`` line, ``doctrine_block``) one per
    REFUSED FIT, so a single token can add several. It drives the warn label
    (through :func:`warning_lines`, which caps what is displayed) and never
    gates a push.
    """
    warnings: list = []
    unresolved = 0
    parts: list[str] = []
    pending = 0            # deferred <br> count (flushed before the next content)
    swallow_next = False   # an omitted line token wants to eat the next newline

    fallback_active = bool(ctx.legacy_fits) and not _has_tag_fit_content(doc, ctx, compact)
    fallback_state = [False]

    def emit(markup: str) -> None:
        nonlocal pending, swallow_next
        parts.append(LINE_BREAK * pending + markup)
        pending = 0
        swallow_next = False

    def newline() -> None:
        nonlocal pending, swallow_next
        if swallow_next:
            swallow_next = False
        else:
            pending += 1

    for run in doc:
        if isinstance(run, TextRun):
            pieces = _escape(run.text).split("\n")
            for i, piece in enumerate(pieces):
                if i:
                    newline()
                if piece:
                    # colour / size go INSIDE a tag: only validated values pass
                    seg = Segment(text=piece, color=_style_color(run.color),
                                  bold=run.bold, italic=run.italic,
                                  underline=run.underline,
                                  size=_style_size(run.size))
                    emit(segments_to_markup([seg]))
        elif isinstance(run, TokenRun):
            markup, warns, unres = _resolve_token(run, ctx, compact, fallback_active, fallback_state)
            warnings.extend(warns)
            unresolved += unres
            if markup:
                emit(markup)
            elif run.kind in _LINE_KINDS:
                swallow_next = True  # omitted line: swallow one following newline

    body = "".join(parts)
    if not body:
        return ResolvedMotd(markup="", warnings=warnings, unresolved=unresolved)
    return ResolvedMotd(markup=f"{_ENVELOPE_HEAD}{body}{_ENVELOPE_TAIL}",
                        warnings=warnings, unresolved=unresolved)


# The envelope :func:`resolve` puts around every non-empty body (byte-identical
# to ``build_motd``'s: white colour + a leading line break). ONE owner for the
# three strings, because the importer below must recognise exactly what the
# renderer writes.
_ENVELOPE_HEAD = f"<color=0xffffffff>{LINE_BREAK}"
_ENVELOPE_TAIL = "</color>"
_ENVELOPE_COLOR = "#ffffff"     # what parse_markup reports for the head's colour


def _styled_chars(segments, default_color=None) -> list:
    """Flatten parsed segments to one entry per character -- ``(char, style)``,
    or ``None`` for a line break -- so two parses can be compared without caring
    how adjacent same-style runs happened to be merged."""
    out: list = []
    for seg in segments:
        if seg.newline:
            out.append(None)
            continue
        style = (seg.color or default_color, seg.bold, seg.italic, seg.underline,
                 seg.size, seg.link)
        out.extend((ch, style) for ch in seg.text)
    return out


def _strip_envelope(markup: str) -> str:
    """``markup`` without :func:`resolve`'s envelope -- or unchanged.

    Importing a MOTD this tool rendered used to turn the envelope into CONTENT
    (a leading ``\\n`` run, and every text run pinned white), so each
    render -> import -> render round grew a blank first line. The envelope is
    dropped only when it is exactly the renderer's: the text starts with the
    head and ends with the tail, the body is non-empty, and -- checked on the
    PARSE, not the text -- the whole markup means precisely "a line break, then
    the body with white as its default colour". A white frame that closes early
    (``<color=0xffffffff><br>a</color>b``), another colour, or a missing close
    is somebody's own styling and is left alone. Nothing a user typed can match:
    typed text is escaped, so it never forms the head.
    """
    if not isinstance(markup, str):
        # Not ours to judge: handed on as it came. ``parse_markup`` reads any
        # FALSY value (None, "") as "no markup" -> an empty document; a truthy
        # non-string is not tolerated there and still raises.
        return markup
    if not (markup.startswith(_ENVELOPE_HEAD) and markup.endswith(_ENVELOPE_TAIL)):
        return markup
    body = markup[len(_ENVELOPE_HEAD):len(markup) - len(_ENVELOPE_TAIL)]
    if not body:
        return markup
    if _styled_chars(parse_markup(markup)) != \
            [None] + _styled_chars(parse_markup(body), _ENVELOPE_COLOR):
        return markup
    return body


# --- chip labels ----------------------------------------------------------

def _ellip(text: str, limit: int = 40) -> str:
    """Truncate ``text`` to ``limit`` chars, ending in ``…`` when cut.

    A non-string (a hand-edited template's ``"name": null`` / a number) is
    clipped as its ``str()`` -- the label the link builders write for it -- so
    neither a chip refresh nor a warning line can raise on one.
    """
    if not isinstance(text, str):
        text = str(text)
    return text if len(text) <= limit else text[: limit - 1] + "…"


# --- the warning label ----------------------------------------------------

WARNING_LINES_SHOWN = 6


def warning_lines(warnings, limit: int = WARNING_LINES_SHOWN) -> list:
    """The warning lines to DISPLAY: de-duplicated, capped, with a count.

    :func:`resolve` reports one line per unresolved pill, and the composer shows
    them all in ONE wrapped label -- unbounded, that label pushes the editor off
    the window and its layout cost grows quadratically. So: duplicates collapse
    (first-seen order kept), at most ``limit`` distinct lines are shown, and the
    rest become a single ``"+<n> more"`` line counting the distinct lines left
    out. Pure; never mutates or stores ``warnings`` -- callers that need the
    full list keep their own (``ResolvedMotd.warnings`` is always complete).
    """
    distinct = list(dict.fromkeys(str(w) for w in (warnings or ())))
    limit = max(0, limit)
    if len(distinct) <= limit:
        return distinct
    return distinct[:limit] + [f"+{len(distinct) - limit} more"]


def token_label(tok: TokenRun, ctx: ResolveContext) -> TokenLabel:
    """Return the :class:`TokenLabel` a pill shows: markup-free text, live/stale,
    delta suffix, and a tooltip describing the current resolution / stale reason.
    """
    kind, p = tok.kind, tok.params

    if kind not in TOKEN_KINDS:
        return TokenLabel(_ellip(kind), False,
                          "unknown token kind (saved by a newer version?)")

    # A pill whose link builder will refuse the target renders as plain text:
    # its chip is UNRESOLVED and the tooltip says there is no link and why --
    # the same target tests ``_resolve_token`` reports a warning for.

    if kind == "char":
        name = p.get("name", "")
        if not is_valid_id(p.get("id")):
            return TokenLabel(_ellip(name), False,
                              f"no character id — shown as plain text: {name}")
        return TokenLabel(_ellip(name), True, f"resolves to character link: {name}")

    if kind == "fit":
        name, dna = p.get("name", ""), p.get("dna", "")
        no_link = f"not a fit link — shown as plain text: {name}"
        if not isinstance(dna, str):
            # A hand-edited template: never DNA, and (as in _finalize_fit) it
            # never reaches the injected providers.
            return TokenLabel(_ellip(name), False, no_link)
        parsed = ctx.parse_fit(dna)
        ok = parsed is not None
        delta = ctx.deltas.get(parsed.ship_type_id, 0) if ok else 0
        sub = _refit_substitute(dna, name, ctx)
        # Is a LINK written at all? Mirror :func:`_finalize_fit`: the target is
        # the canonical form of the (refit-substituted) DNA, and ``fitting_link``
        # refuses one that is not fit DNA. Such a pill renders as its plain
        # label, so the chip is UNRESOLVED (stale style) and must not say
        # "link kept". A DNA-shaped string the catalog cannot parse is not this
        # case: its link IS written and the chip stays solid (below).
        target_parsed = parsed if sub[0] == dna else ctx.parse_fit(sub[0])
        if not is_valid_dna(ctx.canonical_dna(sub[0], target_parsed)):
            return TokenLabel(_ellip(name), False, no_link, delta)
        if sub != (dna, name):
            # The pill is LITERAL on disk but its link resolves to the slot's
            # ACTIVE refit (§13), so the tooltip must not name a fit the MOTD
            # will not contain. The LABEL stays the saved name (the document did
            # not change), and the delta is unaffected: refits are same-hull by
            # rule, so the substitute parses to the same ship_type_id.
            tip = f"follows the active refit → {sub[1]}"
        elif ok:
            tip = f"resolves to fit link: {name}"
        else:
            tip = f"unparseable DNA — link kept, no delta: {name}"
        # SOLID even when the DNA won't parse: the link is kept with the raw DNA
        # (§4.2), only the delta drops — a fit chip is never stale on parse failure.
        return TokenLabel(_ellip(name), True, tip, delta)

    if kind == "system":
        name = p.get("name", "")
        sid = ctx.resolve_system(name)
        ok = sid is not None
        if ok and not is_valid_id(sid):     # the resolver handed back a non-id
            return TokenLabel(_ellip(name), False,
                              f"no system id — shown as plain text: {name}")
        tip = f"resolves to system link: {name}" if ok else \
            f"system '{name}' did not resolve — shown as plain text"
        return TokenLabel(_ellip(name), ok, tip)

    if kind == "channel":
        name = p.get("name", "")
        ok = bool(name)
        return TokenLabel(_ellip(name), ok,
                          f"resolves to channel: {name}" if ok else "channel has no name — omitted")

    if kind == "fc_line":
        # Exactly ``_resolve_token``'s reading, for BOTH sources: the line is
        # rendered only with an id that is not None AND a non-empty name;
        # otherwise it is OMITTED and the chip says so (a selected FC of
        # ``(None, "Bob")`` or ``(7, "")`` is an omitted line, not a plain-text
        # one and not a resolved one).
        if p.get("source") == "pinned":
            cid, name = p.get("id"), p.get("name")
        else:
            cid, name = ctx.fc_selected if ctx.fc_selected else (None, None)
        ok = cid is not None and bool(name)
        name = name or ""
        text = f"FC: {name}" if ok else "FC"
        if ok and not is_valid_id(cid):     # the line renders, the link does not
            return TokenLabel(_ellip(text), False,
                              f"no character id — shown as plain text: FC: {name}")
        tip = f"resolves to: FC: {name}" if ok else "no FC selected — line omitted"
        return TokenLabel(_ellip(text), ok, tip)

    if kind == "staging_line":
        name = p.get("name", "")
        sid = ctx.resolve_system(name)
        ok = sid is not None
        if ok and not is_valid_id(sid):     # the line renders, the link does not
            return TokenLabel(_ellip(f"Staging: {name}"), False,
                              f"no system id — shown as plain text: Staging: {name}")
        tip = f"resolves to: Staging: {name}" if ok else \
            f"system '{name}' did not resolve — line omitted"
        return TokenLabel(_ellip(f"Staging: {name}"), ok, tip)

    if kind == "doctrine_line":
        d = ctx.selected_doctrine
        ok = d is not None
        name = d.name if ok else ""
        text = f"Doctrine: {name}" if ok else "Doctrine"
        tip = f"resolves to: Doctrine: {name}" if ok else "no doctrine selected — line omitted"
        return TokenLabel(_ellip(text), ok, tip)

    if kind == "tag_line":
        tag = p.get("tag", "")
        fbt = ctx.fits_by_tag(False) or {}
        fits = fbt.get(tag) or []
        names = [n for _d, n in fits]
        net = 0
        for dna, _n in fits:
            parsed = ctx.parse_fit(dna)
            if parsed is not None:
                net += ctx.deltas.get(parsed.ship_type_id, 0)
        ok = bool(fits)
        text = f"{tag}: {' | '.join(names)}" if ok else tag
        if ok:
            tip = f"resolves to: {tag}: {' | '.join(names)}"
        else:
            # Actionable stale reason: name the doctrine's ACTUAL tags so a
            # migrated/typo'd tag (e.g. a pre-rename "Logistics" vs the doctrine's
            # "Logi") is one edit away from fixing. NO fuzzy matching — the pill
            # stays unresolved; it is surfaced and explained, not silently guessed.
            avail = [t for t in _ordered_tags(list(fbt.keys())) if fbt.get(t)]
            tip = (f"no fits tagged '{tag}' in this doctrine — this doctrine's tags: "
                   + ", ".join(avail)) if avail else \
                  f"no fits tagged '{tag}' — this doctrine has no tagged fits"
        return TokenLabel(_ellip(text), ok, tip, net)

    if kind == "channel_line":
        label, name = p.get("label", CHANNEL_LINE_DEFAULT_LABEL), p.get("name", "")
        ok = bool(name)
        text = f"{label}: {name}" if ok else label
        tip = f"resolves to: {label}: {name}" if ok else "channel has no name — line omitted"
        return TokenLabel(_ellip(text), ok, tip)

    # doctrine_block
    d = ctx.selected_doctrine
    if d is None:
        return TokenLabel(_ellip("Doctrine block"), False, "no doctrine selected — block omitted")
    fbt = ctx.fits_by_tag(False) or {}
    tagged = [t for t in _ordered_tags(list(fbt.keys())) if fbt.get(t)]
    text = f"Doctrine: {d.name}" + (f" (+{len(tagged)} tags)" if tagged else "")
    return TokenLabel(_ellip(text), True, "resolves to doctrine block: " + ", ".join(tagged))


# --- default doc + migration + import -------------------------------------

def _interleave_newlines(units: list) -> Doc:
    """Join line ``units`` (each a list of runs) with ``\\n`` TextRuns between."""
    runs: Doc = []
    for i, unit in enumerate(units):
        if i:
            runs.append(TextRun("\n"))
        runs.extend(unit)
    return runs


def ordered_tag_lines(tags, fallback: tuple = DEFAULT_TAG_LINES) -> tuple:
    """Order an arbitrary tag list for tag_line emission, de-duped.

    Order-preserving de-dupe, then :func:`motd_builder._ordered_tags`: every tag
    in :data:`motd_builder.TAG_PRIORITY` first (in priority order), then the
    unknown/custom ones in the caller's order — the SAME rule ``build_motd`` and
    ``doctrine_block`` render by, so a tag_line list and the markup it produces
    read in the same sequence. Empty/all-blank input returns ``fallback``.

    Pure: ``tags`` may be any iterable of strings (a store vocabulary, a
    doctrine's member tags). It is the CALLER's job to hand over MIGRATED tag
    names — ``FittingsStore`` rewrites legacy names (``"Support - EWAR"`` →
    ``"EWAR"``) at load, so anything read off a loaded store already is.
    """
    seen: list = []
    for t in (tags or []):
        if t and t not in seen:
            seen.append(t)
    if not seen:
        return tuple(fallback)
    return tuple(_ordered_tags(seen))


def doctrine_tag_lines(doctrine, fallback: tuple = DEFAULT_TAG_LINES) -> tuple:
    """The role tags ``doctrine`` actually uses, ordered for tag_line emission.

    Walks the doctrine's members (duck-typed — ``.members`` each with ``.tags``,
    so this module stays free of a ``fit_models``/``fittings_store`` import) and
    collects the union of their tags, ordered via :func:`ordered_tag_lines`. An
    armor doctrine tagging ewar + tackle therefore starts a fresh document with
    DPS/Logi/Links/EWAR/Tackle lines, and a doctrine using none of those gets no
    empty lines for them.

    ``None`` (no doctrine selected) or a doctrine with no tagged members returns
    ``fallback`` — :data:`DEFAULT_TAG_LINES`, i.e. today's fixed triple, so a
    fresh install with an empty library behaves exactly as it always has.
    """
    tags: list = []
    for member in (getattr(doctrine, "members", None) or []):
        for tag in (getattr(member, "tags", None) or []):
            tags.append(tag)
    return ordered_tag_lines(tags, fallback)


def default_doc(staging_name: str = "", channel: str = "",
                default_tags: tuple = DEFAULT_TAG_LINES) -> Doc:
    """The fresh-tab document — resolves byte-identical to today's ``build_motd``,
    EXCEPT the channel_line label (owner amendment 2026-07-22, spec §4.2: default
    "Channel", was "Logi" — collided with the "Logi:" fits line; see
    :data:`CHANNEL_LINE_DEFAULT_LABEL` and the §4.4 parity test's documented
    single-substitution transform).

    Lines (each on its own line): ``fc_line(selected)`` · ``staging_line`` iff a
    staging name is given · ``doctrine_line`` · one ``tag_line`` per default tag ·
    ``channel_line(CHANNEL_LINE_DEFAULT_LABEL, …)`` iff a channel is remembered.

    ``default_tags`` defaults to :data:`DEFAULT_TAG_LINES` (the historical
    DPS/Logi/Links triple) so every existing caller is unaffected. The wiring
    passes the SELECTED doctrine's real roles instead — build that tuple with
    :func:`doctrine_tag_lines`, which already applies the priority ordering and
    falls back to the triple when there is no doctrine.
    """
    units: list = [[TokenRun("fc_line", {"source": "selected"})]]
    if staging_name:
        units.append([TokenRun("staging_line", {"name": staging_name})])
    units.append([TokenRun("doctrine_line", {})])
    for tag in default_tags:
        units.append([TokenRun("tag_line", {"tag": tag})])
    if channel:
        units.append([TokenRun("channel_line",
                                {"label": CHANNEL_LINE_DEFAULT_LABEL, "name": channel})])
    return _interleave_newlines(units)


def _runs_from_markup(markup: str) -> list:
    """Parse markup into runs — known links become item pills, else styled text.

    Shared by the legacy header/footer migration (:func:`from_legacy_fields`) and
    the MOTD import path (:func:`from_parsed_motd`) so both apply the SAME
    link→token rules. Each segment's ``url=`` target is mapped via
    :func:`_link_to_token` (fitting → ``fit``, showinfo char → ``char``,
    ``showinfo:5`` → ``system``, joinChannel → ``channel``); a target matching no
    pattern (an external ``http`` URL, an unknown showinfo type) keeps its visible
    text as a styled :class:`TextRun` (``html.unescape``d) — the pre-amendment
    flatten behavior. ``<br>`` becomes a ``\\n`` TextRun.
    """
    runs: list = []
    for seg in parse_markup(markup):
        if seg.newline:
            runs.append(TextRun("\n"))
            continue
        # ONE unescape for both uses of the segment text: free text, and a
        # link's LABEL (a pill's ``name``). It is the exact inverse of the
        # ``_escape`` the serialiser applies to both, so a name such as
        # ``A & B`` comes back as itself and never grows an ``&amp;amp;``.
        name = html.unescape(seg.text)
        if seg.link is not None:
            tok = _link_to_token(seg.link, name)
            if tok is not None:
                runs.append(tok)
                continue
        runs.append(TextRun(name, color=seg.color, bold=seg.bold,
                            italic=seg.italic, underline=seg.underline, size=seg.size))
    return runs


def _apply_tag_renames(tags: list, renames: dict) -> list:
    """Map each tag through ``renames`` and de-dupe, preserving order.

    MIRRORS :func:`fittings_store._migrate_tag_list` EXACTLY so the template-tag
    path and the store's membership-tag path cannot drift: a SINGLE
    ``renames.get(t, t)`` lookup (NO chaining — a mapped value is not re-mapped;
    NO case-fold — keys match verbatim), unknown tags pass through unchanged, and
    a legacy+target pair collapses to one entry at the legacy tag's position (the
    target's later duplicate is dropped). ``motd_doc`` stays pure (no
    ``fittings_store`` import) — the rename *table* is injected by the caller.
    """
    out: list = []
    seen: set = set()
    for t in tags:
        new_t = renames.get(t, t)
        if new_t in seen:
            continue
        seen.add(new_t)
        out.append(new_t)
    return out


def from_legacy_fields(saved: dict, tag_renames: dict | None = None) -> Doc:
    """Migrate a v1 ``saved_motds`` entry into a v2 run list (deterministic).

    Order mirrors ``build_motd``'s line list: header runs · ``fc_line(selected)``
    (the wiring sets the FC combo from ``saved['fc']``) · ``staging_line`` iff
    enabled + named · ``doctrine_line`` · a ``tag_line`` per saved tag ·
    ``channel_line("Logi", …)`` iff a channel · footer runs. Kept as "Logi"
    deliberately — the 2026-07-22 amendment that changed the default label to
    :data:`CHANNEL_LINE_DEFAULT_LABEL` ("Channel") applies to NEW creations only;
    this migration path keeps "Logi" so v1 templates render byte-identically to
    what they always produced (see the §4.4 parity test's documented exception).
    Header/footer link markup migrates losslessly to pills (a hand-pasted
    fit/char/system/channel link becomes its live token) via
    :func:`_runs_from_markup`. The saved ``fits``
    fallback pairs are carried separately (as ``ctx.legacy_fits``) and only fire
    when every tag_line resolves empty (see :func:`resolve`); a tag-less template
    that still has saved fits gets one synthetic ``Fits`` tag_line so that
    fallback has an anchor to render at.

    ``tag_renames`` (optional): the store's role-tag rename table
    (``fittings_store._TAG_RENAMES``, injected by the wiring — kept out of this
    pure module). When given, each saved tag is mapped through it via
    :func:`_apply_tag_renames` BEFORE its ``tag_line`` is emitted, so a template
    saved before a rename ("Logistics") lands on the doctrine's current tag
    ("Logi") instead of a permanently-stale pill. ``None`` = no mapping (today's
    verbatim behavior, for callers that don't want store coupling).
    """
    units: list = []
    header = _runs_from_markup(saved.get("header", "") or "")
    if header:
        units.append(header)
    units.append([TokenRun("fc_line", {"source": "selected"})])
    if saved.get("staging_enabled") and (saved.get("staging") or ""):
        units.append([TokenRun("staging_line", {"name": saved.get("staging", "")})])
    units.append([TokenRun("doctrine_line", {})])
    tags = saved.get("tags", []) or []
    if tag_renames:
        tags = _apply_tag_renames(tags, tag_renames)
    if tags:
        for tag in tags:
            units.append([TokenRun("tag_line", {"tag": tag})])
    elif saved.get("fits"):
        # A tag-less v1 template with saved fits: without a tag_line the
        # resolve-time legacy_fits fallback (see :func:`resolve`) has no anchor
        # and the "Fits:" line is silently lost. Emit one synthetic "Fits" anchor
        # tag_line here (where the per-tag lines would have gone). The doctrine
        # has no "Fits" tag so this tag_line resolves empty and the fallback
        # renders "Fits: <links>" at this position — byte-parity with today's
        # {"Fits": loaded} fallback.
        units.append([TokenRun("tag_line", {"tag": "Fits"})])
    channel = saved.get("channel") or ""
    if channel:
        # Literal "Logi" — NOT CHANNEL_LINE_DEFAULT_LABEL. Deliberate v1
        # byte-parity exception (spec §4.2 amendment, 2026-07-22): migrated
        # templates must keep rendering exactly what they always rendered.
        units.append([TokenRun("channel_line", {"label": "Logi", "name": channel})])
    footer = _runs_from_markup(saved.get("footer", "") or "")
    if footer:
        units.append(footer)
    return _interleave_newlines(units)


def _link_to_token(target: str, name: str) -> "TokenRun | None":
    """Map an imported ``url=`` target to an item pill, or None to keep as text."""
    if target.startswith("fitting:"):
        return TokenRun("fit", {"dna": target[len("fitting:"):], "name": name})
    if target.startswith("showinfo:"):
        rest = target[len("showinfo:"):]
        if "//" in rest:
            type_str, id_str = (s.strip() for s in rest.split("//", 1))
            if type_str == "5":  # Solar System typeID → system (id re-resolved at compose)
                return TokenRun("system", {"name": name})
            # character (self-contained id kept). ``parse_id`` accepts exactly
            # what ``char_link`` will write back (ASCII digits, 1..MAX_EVE_ID)
            # and never raises: a superscript digit, an over-long digit string
            # or a zero is "not an id" and the link stays plain text.
            cid = parse_id(id_str)
            if cid is not None:
                return TokenRun("char", {"id": cid, "name": name})
        return None
    if target.startswith("joinChannel:"):
        return TokenRun("channel", {"name": name})
    return None


def from_parsed_motd(raw_markup: str) -> Doc:
    """Parse an imported MOTD's markup into runs (item pills + styled text).

    Known links become ``fit`` / ``char`` / ``system`` / ``channel`` pills; every
    other segment stays styled text (``html.unescape``d); ``<br>`` becomes a
    ``\\n`` TextRun. Line/block structure is not reverse-engineered (line-level
    fidelity only). Shares :func:`_runs_from_markup` with the legacy migration
    path, so header/footer links and imported links resolve identically.

    The renderer's own envelope (white colour + leading break) is dropped first
    (:func:`_strip_envelope`) -- :func:`resolve` adds it back -- so importing a
    MOTD this tool rendered and rendering it again is a fixed point.
    """
    return _runs_from_markup(_strip_envelope(raw_markup))


# --- JSON persistence -----------------------------------------------------

def doc_to_json(doc: Doc) -> list:
    """Serialise a run list to JSON forms (text style stores non-default keys only)."""
    out: list = []
    for run in doc:
        if isinstance(run, TokenRun):
            out.append({"t": "token", "kind": run.kind, "params": dict(run.params)})
        else:
            style: dict = {}
            if run.color is not None:
                style["color"] = run.color
            if run.bold:
                style["bold"] = True
            if run.italic:
                style["italic"] = True
            if run.underline:
                style["underline"] = True
            if run.size is not None:
                style["size"] = run.size
            out.append({"t": "text", "text": run.text, "style": style})
    return out


def doc_from_json(data: list) -> Doc:
    """Rebuild a run list from JSON. Unknown token kinds are preserved verbatim."""
    runs: Doc = []
    for d in data:
        if d.get("t") == "token":
            runs.append(TokenRun(d.get("kind", ""), dict(d.get("params", {}) or {})))
        elif d.get("t") == "text":
            style = d.get("style") or {}
            runs.append(TextRun(d.get("text", ""), color=style.get("color"),
                                bold=style.get("bold", False), italic=style.get("italic", False),
                                underline=style.get("underline", False), size=style.get("size")))
    return runs


def first_staging_name(doc: Doc) -> "str | None":
    """The first ``staging_line`` token's ``name`` param, or None if there is none."""
    for run in doc:
        if isinstance(run, TokenRun) and run.kind == "staging_line":
            return run.params.get("name")
    return None
