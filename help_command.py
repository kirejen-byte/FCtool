"""Fleet-chat ``help FCT`` command: the pure engine (no Tk, no fc_gui).

Typing ``help FCT`` (or ``help FCTool``, any case, the space optional, one
trailing ``?``/``!``/``.`` tolerated) in Fleet chat from one of the owner's OWN
characters pops a toast over that character's client listing every fleet-chat
command FCTool reacts to, with the live keywords and x-up threshold, and a
``(off)`` mark on each command switched off in Settings.

This module owns the config gate, the whole-line matcher and the toast BODY
builder. fc_gui owns two thin seams: ``_help_observe`` (chat poll thread, the
gates) and ``_help_show_toast`` (Tk thread, gathers the live values and shows a
``client_toast.ClientToast``).

Each feature's ``(off)`` state comes from that feature's OWN ``is_enabled`` /
``normalize_config`` -- never re-derived here (the implant reminder once
shipped a ticked box over a feature that could not fire; a help line that
disagreed with the real gate would be the same bug).

Source stays pure ASCII: the two non-ASCII glyphs are built with ``chr()``.

Spec: docs/superpowers/specs/2026-11-22-fleet-chat-help-command-design.md
"""
from __future__ import annotations

import form_command
import range_check
import refit_command

#: House per-key defaulting, mirrored key-for-key in
#: ``default_config.DEFAULT_CONFIG['help_command']``. ON by default: only the
#: owner's own characters can trigger it and all it does is show a toast.
DEFAULTS = {
    "enabled": True,
    "keyword": "help fct",   # whole-line match, spaces optional; BLANK = off
}

#: Per-sender double-send guard (refit's figure). A new toast already
#: replaces the old one per character, so a longer cooldown would only
#: refuse a re-ask after an accidental dismiss.
COOLDOWN_SECONDS = 3.0
#: Toast hold before the fade.
TOAST_SECONDS = 20.0
# Freshness: the SHARED form/refit window (form_command.is_fresh's default,
# MAX_MESSAGE_AGE_S). EVE log times are server UTC, so on a PC whose clock
# runs fast a line's "age" is that skew; a shorter window would silently kill
# help on such a box.

TITLE = "FCTool fleet-chat commands"
#: No snooze is wired, so the default hint's right-click promise is dropped.
HINT = "click to dismiss"
#: Toast width in design px: 56 Consolas-9 glyphs plus the padding.
TOAST_WIDTH = 440

#: Body layout: usage text starts after a 2-space indent and is at most
#: ``USAGE_W`` glyphs; the description starts at column ``DESC_COL`` (0-based)
#: and is at most ``DESC_W`` glyphs, so ``OFF`` always fits in ``MAX_LINE``.
INDENT = "  "
USAGE_W = 22
DESC_COL = 25
DESC_W = 25
MAX_LINE = 56
OFF = " (off)"
FOOTNOTE = "(off) = switched off in Settings > Fleet Command Line"
#: Most role letters listed before the ellipsis.
MAX_ROLES = 5

DOT = chr(0xB7)        # middle dot
ELLIPSIS = chr(0x2026)

#: ``help fctool`` is a fixed alias of the DEFAULT keyword only.
_ALIAS = "helpfctool"


# -- config -------------------------------------------------------------------

def is_enabled(block) -> bool:
    """Is the help command switched on, given a RAW ``config['help_command']``?

    The single master gate. A blank keyword counts as off (nothing could ever
    match it). Absent / ``None`` / malformed inherit the defaults; never
    raises."""
    src = block if isinstance(block, dict) else {}
    if not bool(src.get("enabled", DEFAULTS["enabled"])):
        return False
    keyword = str(src.get("keyword", DEFAULTS["keyword"]) or "").strip()
    return bool(keyword)


def normalize_config(block) -> dict:
    """Coerce ``config['help_command']`` into a fully-populated, sane dict.

    Never raises, never mutates the caller's dict. An ABSENT keyword inherits
    the default; an explicitly BLANK one stays blank (and disables)."""
    src = block if isinstance(block, dict) else {}
    out = dict(DEFAULTS)
    out["enabled"] = bool(src.get("enabled", DEFAULTS["enabled"]))
    out["keyword"] = str(src.get("keyword", DEFAULTS["keyword"]) or "").strip()
    return out


# -- matching -----------------------------------------------------------------

_MARKS = ("?", "!", ".")


def _squash(text) -> str:
    return "".join(str(text or "").casefold().split())


def matches(body, keyword) -> bool:
    """Is ``body`` (one whole chat line) the help command for ``keyword``?

    Whole-line: strip, casefold, collapse whitespace, drop ONE trailing
    ``?``/``!``/``.``, then compare with all spaces removed against the keyword
    with spaces removed and ONE trailing mark dropped too (so a custom keyword
    ``fct?`` matches the line ``fct?``). With the default keyword,
    ``help fctool`` matches too. A blank keyword matches nothing. Never
    raises."""
    try:
        needle = _squash(keyword)
        if needle[-1:] in _MARKS:
            needle = needle[:-1]
        if not needle:
            return False
        text = " ".join(str(body or "").casefold().split())
        if text[-1:] in _MARKS:
            text = text[:-1]
        text = text.replace(" ", "")
        if not text:
            return False
        if text == needle:
            return True
        return needle == _squash(DEFAULTS["keyword"]) and text == _ALIAS
    except Exception:
        return False


# -- the toast body -----------------------------------------------------------

def _fit(text, width) -> str:
    """``text`` cut to ``width`` glyphs, an ellipsis marking the cut. Inner
    spacing is kept (the x-up usage is deliberately ``x  /  x <ship>``);
    callers collapse user-typed words before they get here."""
    text = str(text or "")
    if len(text) <= width:
        return text
    return text[:max(0, width - 1)] + ELLIPSIS


def _row(usage, desc, off=False) -> str:
    desc = _fit(desc, DESC_W) + (OFF if off else "")
    return INDENT + _fit(usage, USAGE_W).ljust(DESC_COL - len(INDENT)) + desc


def _keyword_or_default(normalized, defaults) -> str:
    """The live keyword; a BLANK one (= off) shows the module's DEFAULT, so
    the line still reads as the command it is. Inner whitespace (a pasted
    tab or newline) collapses to one space, like every other user string."""
    keyword = " ".join(str((normalized or {}).get("keyword") or "").split())
    return keyword or " ".join(str(defaults.get("keyword") or "").split())


def _role_usage(role_letters) -> str:
    """``L Logi . D DPS . ...``: unique letters in slot order, at most
    ``MAX_ROLES`` and at most ``USAGE_W`` glyphs, then an ellipsis when any
    were left out. Empty when no slot has a letter."""
    entries, seen = [], set()
    for item in role_letters or ():
        try:
            letter, title = item
        except (TypeError, ValueError):
            continue
        letter = " ".join(str(letter or "").split())
        if not letter or letter.casefold() in seen:
            continue
        seen.add(letter.casefold())
        title = " ".join(str(title or "").split())
        entries.append(f"{letter} {title}" if title else letter)
    if not entries:
        return ""
    sep = f" {DOT} "
    more = f"{sep}{ELLIPSIS}"
    shown = []
    for i, entry in enumerate(entries[:MAX_ROLES]):
        left_out = i + 1 < len(entries)
        candidate = sep.join(shown + [entry]) + (more if left_out else "")
        if len(candidate) > USAGE_W:
            break
        shown.append(entry)
    if not shown:                          # one entry too long on its own
        if len(entries) == 1:              # nothing follows: the full width
            return _fit(entries[0], USAGE_W)
        return _fit(entries[0], USAGE_W - len(more)) + more
    return sep.join(shown) + (more if len(shown) < len(entries) else "")


def _xup_word(xup, field, default):
    """``(word, off)`` for one x-up word. Absent / ``None`` -> the default, not
    off; present but blank (whitespace only) -> the default AND off, because
    the counter then matches nothing. Inner whitespace collapses."""
    raw = xup.get(field)
    if raw is None:
        return default, False
    word = " ".join(str(raw).split())
    return (word, False) if word else (default, True)


def xup_is_off(xup) -> bool:
    """True when the x-up trigger OR clear word is present but blank - the
    same rule ``build_lines`` uses for its ``(off)``. A non-dict block is
    junk, so not off."""
    if not isinstance(xup, dict):
        return False
    return (_xup_word(xup, "trigger_word", "x")[1]
            or _xup_word(xup, "fire_word", "FIRE")[1])


def _help_usage(help_keyword) -> str:
    keyword = " ".join(str(help_keyword or "").split())
    if not keyword or _squash(keyword) == _squash(DEFAULTS["keyword"]):
        return "help FCT"
    return keyword


def build_lines(*, xup, role_letters, range_block, refit_block, form_block,
                help_keyword) -> list[str]:
    """The toast BODY lines (title and hint are separate), plain data in.

    ``xup``: ``{"trigger_word", "fire_word", "threshold"}``;
    ``role_letters``: ``[(letter, title), ...]`` in slot order (empty -> the
    role line is omitted); the three blocks are the RAW config blocks of the
    range check, refit and form commands (``form_block=None`` = no form
    module -> its line is omitted). A command is ``(off)`` iff its OWN
    ``is_enabled`` says so (the range check's blank keyword also counts: its
    engine disables on one). The x-up trigger / clear words have no switch,
    but a BLANK one matches nothing (``xup_counter``), so that line shows the
    default word and ``(off)`` too; an ABSENT word is just the default. The
    footnote appears only when something is off. Every line is at most
    ``MAX_LINE`` glyphs. Never raises on junk input."""
    xup = xup if isinstance(xup, dict) else {}
    trigger, trigger_off = _xup_word(xup, "trigger_word", "x")
    fire, fire_off = _xup_word(xup, "fire_word", "FIRE")
    threshold = xup.get("threshold", 50)
    try:
        threshold = int(threshold)
    except (TypeError, ValueError):
        threshold = 50

    lines = ["Anyone in fleet:",
             _row(f"{trigger}  /  {trigger} <ship>",
                  f"x-up (counts toward {threshold})", trigger_off),
             _row(fire, "reset the x-up count", fire_off)]
    roles = _role_usage(role_letters)
    if roles:
        lines.append(_row(roles, "join that role slot"))
    lines.append(_row("paste burst charges", "update Links coverage"))

    any_off = trigger_off or fire_off
    lines.append("Your characters:")

    rng = range_check.normalize_config(range_block)
    rng_off = (not range_check.is_enabled(range_block)) or not rng["keyword"]
    any_off |= rng_off
    lines.append(_row(
        f"{_keyword_or_default(rng, range_check.DEFAULTS)} [systems]",
        "jump ranges from you", rng_off))

    rf = refit_command.normalize_config(refit_block)
    rf_off = not refit_command.is_enabled(refit_block)
    any_off |= rf_off
    rf_kw = _keyword_or_default(rf, refit_command.DEFAULTS)
    lines.append(_row(f"{rf_kw} <slot> <N|name>", "swap a doctrine refit",
                      rf_off))
    lines.append(_row(f"{rf_kw} {refit_command.RESET_WORD}",
                      "all slots to default", rf_off))

    if form_block is not None:
        fm = form_command.normalize_config(form_block)
        fm_off = not form_command.is_enabled(form_block)
        any_off |= fm_off
        lines.append(_row(
            f"{_keyword_or_default(fm, form_command.DEFAULTS)} <doctrine>",
            "set doctrine + push MOTD", fm_off))

    lines.append(_row(_help_usage(help_keyword), "this list"))
    if any_off:
        lines.append(FOOTNOTE)
    return lines
