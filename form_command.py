"""Fleet-chat "form" command -- the PURE parse/match engine.

Design: ``docs/superpowers/specs/2026-11-22-fleet-chat-form-command-design.md``.
Typing ``form <doctrine>`` in Fleet chat (from one of the owner's own
characters) selects that doctrine in the MOTD composer, loads its top MOTD
template, resets its refits and pushes the MOTD once. This module only decides
WHICH doctrine a line names; the Tk half (gates, apply, push, retry) lives in
``form_action.py`` and the chat hookup in ``fc_gui``.

Everything here is pure: no Tk, no fc_gui, no network, no disk, no threads.
Doctrines arrive DUCK-TYPED (``.id`` / ``.name``), in library order.

Load-bearing decisions, each a place this feature could be silently wrong:

* **Only ``form`` carries a doctrine id.** ``ambiguous`` / ``none`` /
  ``ignore`` have ``doctrine_id is None`` by construction, so a caller that
  blindly does ``if res.doctrine_id: apply()`` cannot act on a failure.
* **The keyword must START the body** and be followed by whitespace, end of
  line or a ``:`` -- ``refit_command.parse``'s rules exactly. "we should form
  up" is chat, "formation" is not "form", and a bare keyword selects nothing.
* **A blank keyword disables the feature** (``is_enabled``), and
  ``normalize_config`` never restores the default over an explicitly blank one.
* **Normalisation:** names and queries are casefolded, apostrophes are
  DROPPED (so "Svipul's" is the one word "svipuls"), and any other
  non-alphanumeric run is a word separator -- "[ARMOR] Muninn" is the words
  ``armor muninn``. Each word compares by its ``word_forms`` (possessive and
  plural folds), so "svipuls" names "Svipul". A fold never shortens a word
  below ``MIN_FORM_LEN`` characters, and a word ending "ss" is never folded.
* **The ladder is EXACT, then PREFIX.** A single whole-name exact match wins
  outright even when other doctrines also prefix-match ("svipuls" forms
  "Svipul", not "Svipul Training"). Two or more exact matches -- including two
  doctrines with the identical normalised name -- ASK; the engine never
  guesses.
* **Casual chat is a SILENT no-op.** With no exact match, a query whose FIRST
  word is shorter than ``MIN_QUERY_LEN`` characters, or that has no letter or
  digit at all, resolves to ``ignore`` (no toast, no work): "form up on the
  titan", "form up please", "form ???", "form :-)". An exact match is checked
  first, so a short-worded doctrine ("T3 Cruisers", "AB Svipul") still forms
  when typed in full. Anything else that matches nothing is ``none`` with the
  message "No doctrine matches '<query>'".
* **Prefix matching is word-anchored and contiguous.** The query's words must
  match a contiguous run of the name's words in order; every word but the last
  must match fully (by forms), the last may be a prefix. "fleet" therefore
  finds "Hurricane Fleet", but "fleet svipul" finds nothing.
* **Only WHOLE words act directly.** A single prefix-ladder hit forms only when
  every query word matched a name word as a whole word (``word_forms``
  intersection: "svipuls" -> "Svipul Fleet"). One that needed a PARTIAL last
  word ("here" -> "Heretic Fleet", "svip" -> "Svipul") is ``ambiguous`` with
  that ONE option -- a click to confirm, because ordinary chat starting with
  the keyword can prefix-match a doctrine by accident.
* **Stale lines never act** (``is_fresh``): the chat monitor resumes from a
  saved position at startup, so a command typed while FCTool was closed would
  otherwise fire on the next launch. Shared with the refit command.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

#: Shape + defaults of ``config['form_command']``. Mirrored in
#: ``default_config.DEFAULT_CONFIG`` (guarded by ``test_default_config.py``);
#: kept here too so the engine self-heals a partially-written block -- house
#: per-key defaulting, never a deep merge.
#:
#: ON by default, the refit command's posture: only the owner's OWN characters
#: can trigger it and a line must START with the keyword. An armed keyword CAN
#: do real work -- an exact or whole-word match switches the composer's
#: doctrine and pushes the MOTD -- so ordinary chat is fenced off by the
#: noise gate (``ignore``), the whole-word rule (a partial match only asks) and
#: the age gate (``is_fresh``); see the module docstring.
DEFAULTS = {
    "enabled": True,
    "keyword": "form",       # must START the body; BLANK = disabled
}

#: Shortest query (and, with no exact match, shortest word in it) that is
#: treated as a doctrine fragment. Below it the line is ordinary chat.
MIN_QUERY_LEN = 3
#: A possessive/plural fold never produces a form shorter than this.
MIN_FORM_LEN = 3
#: A chat line older than this (seconds, by its log timestamp) is never acted
#: on: it was replayed from the log, not typed just now.
MAX_MESSAGE_AGE_S = 60

#: Every ``FormMatch.kind``. ``form`` is the ONLY one that carries an id.
KINDS = ("form", "ambiguous", "none", "ignore")

#: Apostrophes dropped before splitting a name into words: ASCII ``'`` and the
#: typographic right single quotation mark (U+2019, built at runtime so the
#: source stays pure ASCII).
_APOSTROPHES = ("'", chr(0x2019))


# -- config -------------------------------------------------------------------

def is_enabled(block) -> bool:
    """Is the form command switched on, given a RAW ``config['form_command']``?

    The SINGLE master gate. A blank keyword counts as off: nothing could ever
    match it. Absent / ``None`` / malformed inherit the defaults; never
    raises."""
    src = block if isinstance(block, dict) else {}
    if not bool(src.get("enabled", DEFAULTS["enabled"])):
        return False
    keyword = str(src.get("keyword", DEFAULTS["keyword"]) or "").strip()
    return bool(keyword)


def normalize_config(block) -> dict:
    """Coerce ``config['form_command']`` into a fully-populated, sane dict.

    Never raises, never returns a partial shape, never mutates the caller's
    dict. An ABSENT keyword inherits the default; an explicitly BLANK one stays
    blank (and disables the feature)."""
    src = block if isinstance(block, dict) else {}
    out = dict(DEFAULTS)
    out["enabled"] = bool(src.get("enabled", DEFAULTS["enabled"]))
    out["keyword"] = str(src.get("keyword", DEFAULTS["keyword"]) or "").strip()
    return out


# -- parsing ------------------------------------------------------------------

def parse(body, keyword) -> str | None:
    """``"form  svipuls "`` -> ``"svipuls"``; anything else -> ``None``.

    The body must START with ``keyword`` (case-insensitive, after ``lstrip``)
    and the keyword must be followed by end of line, whitespace, or a ``:``
    ("form: svipul" and "form :svipul" alike). A bare keyword is not a command,
    and neither is a blank keyword. The returned query keeps the typed case
    (it is echoed in the "No doctrine matches" toast) with its whitespace
    collapsed."""
    needle = str(keyword or "").strip().lower()
    if not needle:
        return None
    text = str(body or "").lstrip()
    if not text.lower().startswith(needle):
        return None
    rest = text[len(needle):]
    if rest and not rest[:1].isspace() and rest[:1] != ":":
        return None                      # "formation", not "form"
    rest = rest.lstrip()
    if rest[:1] == ":":
        rest = rest[1:]
    words = rest.split()
    if not words:
        return None                      # bare keyword selects nothing
    return " ".join(words)


def is_fresh(ts, now=None, max_age_s=MAX_MESSAGE_AGE_S) -> bool:
    """Was a chat line with log timestamp ``ts`` typed within ``max_age_s``?

    The stale-log age gate shared by the form and refit commands: the chat
    monitor resumes from a saved position at startup, so a command typed while
    FCTool was closed is delivered on the next launch and must not fire.

    EVE log times are UTC and ``chat_monitor`` parses them NAIVE, so a naive
    ``ts`` is compared against naive UTC now (never local time) and an aware
    one against aware UTC now. Fail-OPEN: a missing or non-datetime ``ts``, or
    any comparison error (naive vs aware ``now``), counts as fresh. A future
    timestamp (clock skew) is fresh. Never raises."""
    if not isinstance(ts, datetime):
        return True
    try:
        if now is None:
            now = datetime.now(timezone.utc)
            if ts.tzinfo is None:
                now = now.replace(tzinfo=None)
        age = (now - ts).total_seconds()
        return age <= float(max_age_s)
    except Exception:
        return True


# -- normalisation ------------------------------------------------------------

def _fold_plural(word) -> set:
    """Plural folds of an (already possessive-stripped) casefolded word."""
    out = set()
    if word.endswith("ss"):
        return out                       # "glass", "moss": never folded
    if word.endswith("ies") and len(word) > 4:
        out.add(word[:-3] + "y")
    if word.endswith("es"):
        out.add(word[:-2])
    if word.endswith("s"):
        out.add(word[:-1])
    return out


def word_forms(word) -> frozenset:
    """Every form ``word`` may compare as: itself (casefolded), minus a
    trailing ``'s`` possessive, and its plural folds (``ies`` -> ``y`` for
    words longer than 4, minus ``es``, minus ``s``; never for ``ss``).

    No fold shorter than ``MIN_FORM_LEN`` is produced; the word itself always
    is, however short."""
    base = str(word or "").casefold()
    forms = {base}
    stem = base
    for apostrophe in _APOSTROPHES:
        suffix = apostrophe + "s"
        if stem.endswith(suffix):
            stem = stem[:-len(suffix)]
            break
    candidates = {stem} | _fold_plural(stem)
    forms.update(c for c in candidates if len(c) >= MIN_FORM_LEN)
    return frozenset(forms)


def tokenize(name) -> tuple:
    """``"[ARMOR] Muninn"`` -> ``("armor", "muninn")``.

    Casefold, drop apostrophes (a possessive stays part of its word), split on
    every other non-alphanumeric run, drop empties."""
    text = str(name or "").casefold()
    for apostrophe in _APOSTROPHES:
        text = text.replace(apostrophe, "")
    words, current = [], []
    for ch in text:
        if ch.isalnum():
            current.append(ch)
        elif current:
            words.append("".join(current))
            current = []
    if current:
        words.append("".join(current))
    return tuple(words)


# -- matching -----------------------------------------------------------------

@dataclass(frozen=True)
class FormMatch:
    """What a query means against the doctrine library.

    ``kind`` is one of ``KINDS``. INVARIANT: only ``form`` carries a
    ``doctrine_id``; ``ambiguous`` carries ``options`` = ``(id, name)`` pairs
    in library order; ``none`` carries the toast ``message``; ``ignore`` is a
    silent no-op."""
    kind: str
    doctrine_id: str | None = None
    options: tuple[tuple[str, str], ...] = field(default=())
    message: str = ""


def _entries(doctrines) -> list:
    """``(id, name, word-forms tuple)`` for every usable doctrine, in order.

    A doctrine with no id cannot be acted on and one whose name has no words
    cannot be matched; both are skipped rather than raising -- this runs on
    whatever the library hands back."""
    out = []
    try:
        items = list(doctrines or ())
    except Exception:
        return out
    for doctrine in items:
        doctrine_id = getattr(doctrine, "id", None)
        if not doctrine_id:
            continue
        name = str(getattr(doctrine, "name", "") or "")
        words = tokenize(name)
        if not words:
            continue
        out.append((doctrine_id, name, tuple(word_forms(w) for w in words)))
    return out


def _is_exact(query_forms, name_forms) -> bool:
    return (len(query_forms) == len(name_forms)
            and all(q & n for q, n in zip(query_forms, name_forms)))


def _word_prefixes(query_word_forms, name_word_forms) -> bool:
    return any(n.startswith(q)
               for q in query_word_forms for n in name_word_forms)


def _prefix_kind(query_forms, name_forms):
    """Do the query's words prefix-match a contiguous run of the name's?

    ``"whole"`` when some run matches every query word as a WHOLE word (forms
    intersect), ``"partial"`` when a run matches only with the last query word
    as a prefix, else None."""
    k = len(query_forms)
    found = None
    for start in range(len(name_forms) - k + 1):
        run = name_forms[start:start + k]
        if not all(q & n for q, n in zip(query_forms[:-1], run[:-1])):
            continue
        if query_forms[-1] & run[-1]:
            return "whole"
        if _word_prefixes(query_forms[-1], run[-1]):
            found = "partial"
    return found


def _verdict(hits) -> FormMatch:
    if len(hits) == 1:
        return FormMatch(kind="form", doctrine_id=hits[0][0])
    return _ask(hits)


def _ask(hits) -> FormMatch:
    return FormMatch(kind="ambiguous",
                     options=tuple((i, n) for i, n, _f in hits),
                     message="Which doctrine?")


def match(doctrines, query) -> FormMatch:
    """Which doctrine does ``query`` name? Pure; mutates nothing; never raises.

    Ladder: (a) whole-name EXACT (same word count, every word pair shares a
    form) -- one hit forms, several ask; (b) the noise gate: no letter/digit,
    or a first word shorter than ``MIN_QUERY_LEN`` -> ``ignore``; (c)
    word-anchored PREFIX -- one WHOLE-word hit forms, one partial hit asks
    with that single option, several ask; none -> ``none`` with the toast
    message."""
    text = " ".join(str(query or "").split())
    words = tokenize(text)
    query_forms = tuple(word_forms(w) for w in words)
    entries = _entries(doctrines)

    if query_forms:
        exact = [e for e in entries if _is_exact(query_forms, e[2])]
        if exact:
            return _verdict(exact)

    if not words or len(words[0]) < MIN_QUERY_LEN:
        return FormMatch(kind="ignore")

    hits, whole = [], []
    for entry in entries:
        kind = _prefix_kind(query_forms, entry[2])
        if kind is not None:
            hits.append(entry)
            if kind == "whole":
                whole.append(entry)
    if len(hits) == 1 and not whole:
        return _ask(hits)            # a partial word only ever asks
    if hits:
        return _verdict(hits)
    return FormMatch(kind="none", message=f"No doctrine matches '{text}'")
