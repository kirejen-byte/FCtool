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
    double-escapes. Fit / character / system / channel *names* are emitted raw
    inside their links, exactly as :func:`motd_builder.build_motd` does.

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


# --- fit finalisation (canonical DNA + delta lookup) ----------------------

def _finalize_fit(dna: str, name: str, ctx: ResolveContext):
    """Return ``(markup, delta, parsed_ok)`` for one fit, matching ``build_motd``.

    Canonicalises the DNA, looks up the ideal-fleet delta by the parsed ship
    type, and emits ``fitting_link(canon, name) + delta_markup(delta)``. An
    unparseable DNA keeps the raw DNA and drops the delta (today's behavior).
    """
    parsed = ctx.parse_fit(dna)
    canon = ctx.canonical_dna(dna, parsed)
    delta = ctx.deltas.get(parsed.ship_type_id, 0) if parsed is not None else 0
    return fitting_link(canon, name) + delta_markup(delta), delta, parsed is not None


def _join_fits(fits, ctx: ResolveContext) -> str:
    return " | ".join(_finalize_fit(dna, name, ctx)[0] for dna, name in fits)


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
        return char_link(p.get("id"), p.get("name", "")), [], 0

    if kind == "fit":
        # A fit ALWAYS resolves: an unparseable DNA keeps the raw DNA and drops
        # the delta (§4.2 "DNA unparseable → raw DNA kept, no delta"; §9 "degrades
        # to … raw DNA"). It is never counted unresolved / rendered stale merely
        # because the type-catalog could not parse it.
        markup, _delta, _ok = _finalize_fit(p.get("dna", ""), p.get("name", ""), ctx)
        return markup, [], 0

    if kind == "system":
        name = p.get("name", "")
        sid = ctx.resolve_system(name)
        if sid is not None:
            return system_link(sid, name), [], 0
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
            return f"FC: {char_link(cid, name)}", [], 0
        return "", [_FC_WARN], 1

    if kind == "staging_line":
        name = p.get("name", "")
        sid = ctx.resolve_system(name)
        if sid is not None:
            return f"Staging: {system_link(sid, name)}", [], 0
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
            return f"{_escape(tag)}: {_join_fits(fits, ctx)}", [], 0
        # empty tag: fall back to the legacy fits at the FIRST empty tag_line.
        if fallback_active and not fallback_state[0]:
            fallback_state[0] = True
            return f"Fits: {_join_fits(ctx.legacy_fits or [], ctx)}", [], 0
        if fallback_active:
            return "", [], 0  # remaining empty tag_lines: silent (fits already shown)
        return "", [f"{tag} pill unresolved — its line was omitted"], 1

    if kind == "channel_line":
        name = p.get("name", "")
        if not name:
            return "", [], 0  # omit silently (matches today's ``if channel:`` guard)
        label = p.get("label", "Logi")
        return f"{_escape(label)}: {channel_text(name, ctx.resolve_channel_id(name))}", [], 0

    if kind == "doctrine_block":
        d = ctx.selected_doctrine
        if d is None:
            return "", [_DOCTRINE_WARN], 1
        fbt = ctx.fits_by_tag(compact) or {}
        lines = [f"Doctrine: {_escape(d.name)}"]
        for tag in _ordered_tags(list(fbt.keys())):
            fits = fbt.get(tag) or []
            if fits:
                lines.append(f"{_escape(tag)}: {_join_fits(fits, ctx)}")
        return LINE_BREAK.join(lines), [], 0

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


# --- resolution walk ------------------------------------------------------

def resolve(doc: Doc, ctx: ResolveContext, compact: bool = False) -> ResolvedMotd:
    """Serialise ``doc`` to MOTD markup, resolving every token against ``ctx``.

    Free text is escaped and style-wrapped; tokens resolve per §4.2. Omitted
    line/block tokens leave no blank line and never a dangling break. The
    non-empty body is wrapped exactly like :func:`motd_builder.build_motd`
    (leading ``<br>`` + white colour). Returns the markup, any warnings, and the
    count of stale/omitted tokens (drives the warn label; never gates push).
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
                    seg = Segment(text=piece, color=run.color, bold=run.bold,
                                  italic=run.italic, underline=run.underline, size=run.size)
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
    return ResolvedMotd(markup=f"<color=0xffffffff>{LINE_BREAK}{body}</color>",
                        warnings=warnings, unresolved=unresolved)


# --- chip labels ----------------------------------------------------------

def _ellip(text: str, limit: int = 40) -> str:
    """Truncate ``text`` to ``limit`` chars, ending in ``…`` when cut."""
    return text if len(text) <= limit else text[: limit - 1] + "…"


def token_label(tok: TokenRun, ctx: ResolveContext) -> TokenLabel:
    """Return the :class:`TokenLabel` a pill shows: markup-free text, live/stale,
    delta suffix, and a tooltip describing the current resolution / stale reason.
    """
    kind, p = tok.kind, tok.params

    if kind not in TOKEN_KINDS:
        return TokenLabel(_ellip(kind), False,
                          "unknown token kind (saved by a newer version?)")

    if kind == "char":
        name = p.get("name", "")
        return TokenLabel(_ellip(name), True, f"resolves to character link: {name}")

    if kind == "fit":
        name, dna = p.get("name", ""), p.get("dna", "")
        parsed = ctx.parse_fit(dna)
        ok = parsed is not None
        delta = ctx.deltas.get(parsed.ship_type_id, 0) if ok else 0
        tip = f"resolves to fit link: {name}" if ok else f"unparseable DNA — link kept, no delta: {name}"
        # SOLID even when the DNA won't parse: the link is kept with the raw DNA
        # (§4.2), only the delta drops — a fit chip is never stale on parse failure.
        return TokenLabel(_ellip(name), True, tip, delta)

    if kind == "system":
        name = p.get("name", "")
        ok = ctx.resolve_system(name) is not None
        tip = f"resolves to system link: {name}" if ok else \
            f"system '{name}' did not resolve — shown as plain text"
        return TokenLabel(_ellip(name), ok, tip)

    if kind == "channel":
        name = p.get("name", "")
        ok = bool(name)
        return TokenLabel(_ellip(name), ok,
                          f"resolves to channel: {name}" if ok else "channel has no name — omitted")

    if kind == "fc_line":
        if p.get("source") == "pinned":
            name, ok = p.get("name") or "", p.get("id") is not None and bool(p.get("name"))
        else:
            ok = bool(ctx.fc_selected)
            name = ctx.fc_selected[1] if ctx.fc_selected else ""
        text = f"FC: {name}" if ok else "FC"
        tip = f"resolves to: FC: {name}" if ok else "no FC selected — line omitted"
        return TokenLabel(_ellip(text), ok, tip)

    if kind == "staging_line":
        name = p.get("name", "")
        ok = ctx.resolve_system(name) is not None
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
        label, name = p.get("label", "Logi"), p.get("name", "")
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


def default_doc(staging_name: str = "", channel: str = "",
                default_tags: tuple = ("DPS", "Logi", "Links")) -> Doc:
    """The fresh-tab document — resolves byte-identical to today's ``build_motd``.

    Lines (each on its own line): ``fc_line(selected)`` · ``staging_line`` iff a
    staging name is given · ``doctrine_line`` · one ``tag_line`` per default tag ·
    ``channel_line("Logi", …)`` iff a channel is remembered.
    """
    units: list = [[TokenRun("fc_line", {"source": "selected"})]]
    if staging_name:
        units.append([TokenRun("staging_line", {"name": staging_name})])
    units.append([TokenRun("doctrine_line", {})])
    for tag in default_tags:
        units.append([TokenRun("tag_line", {"tag": tag})])
    if channel:
        units.append([TokenRun("channel_line", {"label": "Logi", "name": channel})])
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
    ``channel_line("Logi", …)`` iff a channel · footer runs. Header/footer link
    markup migrates losslessly to pills (a hand-pasted fit/char/system/channel
    link becomes its live token) via :func:`_runs_from_markup`. The saved ``fits``
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
            if id_str.isdigit():  # character (self-contained id kept)
                return TokenRun("char", {"id": int(id_str), "name": name})
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
    """
    return _runs_from_markup(raw_markup)


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
