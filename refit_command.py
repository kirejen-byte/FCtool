"""Fleet-chat "refit" command — the PURE parse/resolve engine.

Design: ``docs/superpowers/specs/2026-09-11-doctrine-refits-design.md`` §14.
Typing ``refit <selector>`` in Fleet chat swaps a doctrine slot's ACTIVE refit
(see §4: ``DoctrineMember.refits`` is the ordered fit-id list, ``[0]`` is the
default, ``member.fit_id`` is the active one).

Everything in this module is pure: no Tk, no fc_gui, no network, no disk, no
threads. Doctrines and fits arrive DUCK-TYPED (``.id``/``.name``/``.members``;
``.fit_id``/``.refits``/``.tags``; ``.id``/``.name``/``.hull_name``), and fits
are fetched through an injected ``get_fit`` callable, so the whole grammar is
testable with no store and no library. The Tk half lives in ``refit_toast.py``;
the chat-thread wiring lives in ``fc_gui``.

Load-bearing decisions, each a place this feature could be silently wrong:

* **Default failure = NO CHANGE.** Every ambiguity, every miss, every malformed
  line resolves to ``ambiguous`` / ``noop`` / ``error`` — kinds that carry
  ``fit_id is None`` by construction, so a caller that blindly does
  ``if res.fit_id: swap()`` cannot swap on a failure. A mistyped command in
  fleet chat must never mutate the owner's doctrine library.
* **The keyword must START the body**, unlike ``range_check.matches_keyword``'s
  substring rule. A range check is a bare keyword and can ride inside a
  sentence; a refit command carries ARGUMENTS, so "we should refit 2 of those"
  would otherwise parse as a real swap. The keyword must also be followed by
  whitespace or end-of-line, so "refitting" is not "refit".
* **A blank keyword disables the feature** (``is_enabled``), the same second
  off-switch ``range_check`` has, and ``normalize_config`` never substitutes
  the default over an explicitly blank one.
* **The slot filter is tried on the FIRST token only.** A tag (exact,
  case-insensitive) beats a hull-name prefix, and the hull prefix needs >= 3
  characters — the ``resolve_partial_name`` floor, for the same reason: short
  fragments are ordinary chat words far more often than they are ship names.
* **The name ladder is exact -> prefix -> substring**, mirroring
  ``range_check.resolve_partial_name``, but substring IS allowed here: fit names
  are free text ("Muninn Grappler/AC") and the pool is a handful of fits the
  owner wrote themselves, not 8,000 system names.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

#: Shape + defaults of ``config['refit_command']``. Mirrored in
#: ``default_config.DEFAULT_CONFIG`` by the wiring (guarded by
#: ``test_default_config.py``); kept here too so the engine self-heals a
#: partially-written block — house per-key defaulting, never a deep merge.
#:
#: ON by default, unlike the range check: this one is gated to the FC's OWN
#: characters AND a line that does not start with the keyword is a no-op by
#: contract, so the blast radius of leaving it armed is a toast.
DEFAULTS = {
    "enabled": True,
    "keyword": "refit",      # must START the body; BLANK = disabled
}

#: Per-sender cooldown. Short on purpose — this is a deliberate command, not a
#: broadcast trigger; the window only exists so a double-send (or the chat
#: tail re-reading a line) cannot double-swap.
COOLDOWN_SECONDS = 3.0

#: Minimum length of a name-fragment selector. Below this the pool is a
#: coin-flip and the answer would be arbitrary.
MIN_FRAGMENT_LEN = 3
#: Minimum length of a hull-name PREFIX used as a slot filter (the
#: ``range_check.MIN_PARTIAL_SHAPED_PREFIX_LEN`` floor, same reasoning).
MIN_HULL_PREFIX_LEN = 3

#: The word that means "put every slot back on its default refit".
RESET_WORD = "reset"

#: Every ``Resolution.kind``. ``swap`` is the ONLY one that carries a fit id.
KINDS = ("swap", "reset", "ambiguous", "noop", "error")

#: Rendered between the slot's hull and the fit name in every label/message.
ARROW = "→"          # →


# ── config ───────────────────────────────────────────────────────────────────

def is_enabled(block) -> bool:
    """Is the refit command switched on, given a RAW ``config['refit_command']``?

    The SINGLE master gate — the Settings checkbox and the chat hookup must
    never re-derive it independently (``range_check.is_enabled``'s rule, and
    the implant reminder's shipped bug before it). A blank keyword counts as
    off: with no keyword there is nothing that could ever match, so a ticked
    box over a blank keyword must not read as armed. Absent / ``None`` /
    malformed inherit the defaults; never raises."""
    src = block if isinstance(block, dict) else {}
    if not bool(src.get("enabled", DEFAULTS["enabled"])):
        return False
    keyword = str(src.get("keyword", DEFAULTS["keyword"]) or "").strip()
    return bool(keyword)


def normalize_config(block) -> dict:
    """Coerce ``config['refit_command']`` into a fully-populated, sane dict.

    Never raises, never returns a partial shape, never mutates the caller's
    dict. An ABSENT keyword inherits the default; an explicitly BLANK one stays
    blank (and disables the feature) — restoring the default over a keyword the
    owner deliberately cleared would re-arm what they switched off."""
    src = block if isinstance(block, dict) else {}
    out = dict(DEFAULTS)
    out["enabled"] = bool(src.get("enabled", DEFAULTS["enabled"]))
    out["keyword"] = str(src.get("keyword", DEFAULTS["keyword"]) or "").strip()
    return out


# ── parsing ──────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Command:
    """One parsed command line.

    ``slot`` is ALWAYS ``None`` out of ``parse``: deciding whether the first
    token names a slot needs the doctrine (a tag or a hull), which the parser
    does not have. ``resolve`` makes that call and reports it on the
    ``Resolution``. The field exists so a caller that builds a Command by hand
    (tests, a future GUI path) can pin the slot explicitly.

    ``tokens`` is the whitespace split of everything after the keyword — the
    ONE place ``resolve`` reads the command's words from, so slot-vs-selector
    partitioning never has to re-split ``selector``."""
    slot: str | None = None
    selector: str | None = None
    reset: bool = False
    tokens: tuple[str, ...] = field(default=())


def parse(body, keyword) -> Command | None:
    """``"refit 2"`` -> ``Command``; anything that is not a command -> ``None``.

    The body must START with ``keyword`` (case-insensitive, after ``lstrip``)
    and the keyword must be followed by end-of-line or whitespace. Substring
    matching — which is what the range check does — is WRONG here: a refit
    command carries arguments, so "we should refit 2 of those" would otherwise
    swap a fit. A bare keyword with no arguments is not a command either
    (there is nothing to select), and neither is a blank keyword."""
    needle = str(keyword or "").strip().lower()
    if not needle:
        return None
    text = str(body or "").lstrip()
    if not text.lower().startswith(needle):
        return None
    rest = text[len(needle):]
    if rest and not rest[:1].isspace():
        return None                      # "refitting", not "refit"
    tokens = tuple(rest.split())
    if not tokens:
        return None                      # bare keyword selects nothing
    if len(tokens) == 1 and tokens[0].lower() == RESET_WORD:
        return Command(slot=None, selector=None, reset=True, tokens=tokens)
    return Command(slot=None, selector=" ".join(tokens), reset=False,
                   tokens=tokens)


# ── resolution ───────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Resolution:
    """What a command means against a doctrine. ``kind`` is one of ``KINDS``.

    INVARIANT: only ``swap`` carries a ``fit_id``; every other kind has
    ``fit_id is None``, which is what makes "default failure = no change"
    structural rather than a discipline the caller has to keep."""
    kind: str
    fit_id: str | None = None
    slot_label: str = ""
    fit_label: str = ""
    options: tuple[tuple[str, str], ...] = field(default=())
    message: str = ""


def _error(message) -> Resolution:
    return Resolution(kind="error", message=str(message))


def _noop(message) -> Resolution:
    return Resolution(kind="noop", message=str(message))


def slot_refits(member) -> list:
    """The slot's ordered refit fit ids, or ``[]`` for a plain member.

    ``getattr`` + ``or []`` because a member built by an older loader has no
    ``refits`` attribute at all, and because ``None`` is what a hand-edited
    library can put there."""
    return list(getattr(member, "refits", None) or [])


def _slot_hull(member, get_fit) -> str:
    """Display name of the slot's ship.

    Normally the ACTIVE fit's ``hull_name``. A missing active fit (deleted from
    the library, or a hand-edited file) falls back to the first refit that does
    resolve, so the FC still reads "Muninn" rather than a blank — the label is
    for humans, and a slot whose every fit is missing is unaddressable anyway."""
    for fit_id in [getattr(member, "fit_id", None)] + slot_refits(member):
        if not fit_id:
            continue
        fit = _fit_of(get_fit, fit_id)
        hull = str(getattr(fit, "hull_name", "") or "") if fit else ""
        if hull:
            return hull
    return "ship"


def _fit_of(get_fit, fit_id):
    """``get_fit(fit_id)`` with every failure flattened to ``None``.

    The store's getter is a plain dict read today, but this engine also runs
    against a snapshot taken on the chat thread; a getter that raises must
    degrade to "that fit is missing" rather than killing the command."""
    if fit_id is None:
        return None
    try:
        return get_fit(fit_id)
    except Exception:
        return None


def _fit_name(fit, fallback="") -> str:
    return str(getattr(fit, "name", "") or "") or fallback


def _option_label(hull, fit_name) -> str:
    return f"{hull} {ARROW} {fit_name}"


def _refit_slots(doctrine) -> list:
    """Every member of ``doctrine`` that has refits, in doctrine order."""
    members = getattr(doctrine, "members", None) or []
    return [m for m in members if slot_refits(m)]


def _tags_of(member) -> list:
    return [str(t) for t in (getattr(member, "tags", None) or [])]


def _slot_filter(token, slots, get_fit) -> list:
    """Slots named by ``token``, or ``[]`` when it names no slot.

    A tag match (exact, case-insensitive) is tried first and wins outright; a
    hull-name PREFIX of >= ``MIN_HULL_PREFIX_LEN`` characters is the fallback.
    Ordered that way because tags are a closed vocabulary the owner typed and
    hull prefixes are guesswork: "DPS" must never be read as a ship whose name
    happens to start with those letters."""
    needle = str(token or "").strip().lower()
    if not needle:
        return []
    by_tag = [m for m in slots
              if any(t.strip().lower() == needle for t in _tags_of(m))]
    if by_tag:
        return by_tag
    if len(needle) < MIN_HULL_PREFIX_LEN:
        return []
    return [m for m in slots
            if _slot_hull(m, get_fit).lower().startswith(needle)]


def _swap_or_noop(member, fit_id, fit, get_fit) -> Resolution:
    """The one place a chosen fit becomes a verdict.

    Re-picking the fit that is already active is a ``noop``, never a swap: the
    store would refuse to bump ``modified`` anyway (§5), and telling the FC
    "already active" is more useful than a success toast for a no-change."""
    hull = _slot_hull(member, get_fit)
    name = _fit_name(fit, fallback=str(fit_id))
    if str(getattr(member, "fit_id", "") or "") == str(fit_id):
        return _noop(f"{name} is already active")
    return Resolution(kind="swap", fit_id=fit_id, slot_label=hull,
                      fit_label=name, message=f"Refit: {hull} {ARROW} {name}")


def _resolve_number(number, slots, get_fit) -> Resolution:
    """``refit [<slot>] <N>`` — the 1-based pick, ``1`` being the default refit."""
    if len(slots) == 1:
        member = slots[0]
        refits = slot_refits(member)
        hull = _slot_hull(member, get_fit)
        if not 1 <= number <= len(refits):
            return _error(f"{hull} has only {len(refits)} refits")
        fit_id = refits[number - 1]
        fit = _fit_of(get_fit, fit_id)
        if fit is None:
            return _error(f"refit {number} is missing from the library")
        return _swap_or_noop(member, fit_id, fit, get_fit)

    # Several candidate slots: one option per slot that HAS an Nth refit. A
    # slot with fewer refits (or whose Nth fit is missing from the library) is
    # skipped rather than reported — the FC asked every DPS ship for its
    # number 2, and a slot without one simply has no answer to offer.
    picks = []
    for member in slots:
        refits = slot_refits(member)
        if not 1 <= number <= len(refits):
            continue
        fit_id = refits[number - 1]
        fit = _fit_of(get_fit, fit_id)
        if fit is None:
            continue
        picks.append((member, fit_id, fit))
    return _from_picks(picks, get_fit,
                       empty_message=f"no slot has a refit {number}")


def _resolve_fragment(fragment, slots, get_fit) -> Resolution:
    """``refit [<slot>] <name fragment>`` — the exact/prefix/substring ladder.

    Pool = every fit of every candidate slot (a slot's ``refits`` list includes
    its active fit, per the §4 invariant). Missing fits are skipped: a fit that
    is not in the library has no name to match on."""
    needle = fragment.strip().lower()
    if len(needle) < MIN_FRAGMENT_LEN:
        return _error("type at least 3 letters of the fit name")

    pool = []
    for member in slots:
        for fit_id in slot_refits(member):
            fit = _fit_of(get_fit, fit_id)
            if fit is None:
                continue
            name = _fit_name(fit)
            if not name:
                continue
            pool.append((member, fit_id, fit, name.lower()))

    for stage in ("exact", "prefix", "substring"):
        if stage == "exact":
            hits = [p for p in pool if p[3] == needle]
        elif stage == "prefix":
            hits = [p for p in pool if p[3].startswith(needle)]
        else:
            hits = [p for p in pool if needle in p[3]]
        if hits:
            return _from_picks([(m, fid, fit) for m, fid, fit, _n in hits],
                               get_fit, empty_message="")
    return _error(f"no refit matches '{fragment.strip()}'")


def _from_picks(picks, get_fit, *, empty_message) -> Resolution:
    """Turn candidate ``(member, fit_id, fit)`` triples into a Resolution.

    Exactly one candidate resolves (swap, or noop when it is already active);
    several become ``ambiguous`` with one clickable option each; none becomes
    an error. A filter that happens to leave ONE candidate standing resolves it
    rather than posting a single-option "which did you mean?" — the FC named
    something unique, the fact that other slots were considered is invisible."""
    if not picks:
        return _error(empty_message)
    if len(picks) == 1:
        member, fit_id, fit = picks[0]
        return _swap_or_noop(member, fit_id, fit, get_fit)
    options = tuple(
        (fit_id, _option_label(_slot_hull(member, get_fit), _fit_name(fit)))
        for member, fit_id, fit in picks)
    return Resolution(kind="ambiguous", options=options,
                      message="Which refit?")


def resolve(doctrine, get_fit, command) -> Resolution:
    """What ``command`` means against ``doctrine``. Pure; mutates nothing.

    ``get_fit`` is ``fit_id -> fit`` (``None`` for a fit that is not in the
    library). The returned ``Resolution`` is the caller's ENTIRE instruction:
    only ``kind == "swap"`` names a fit to activate, and only ``kind ==
    "reset"`` asks for the reset-to-defaults sweep."""
    if command is None:
        return _error("no refit command")
    if doctrine is None:
        return _error("no active doctrine")

    slots = _refit_slots(doctrine)
    if not slots:
        name = str(getattr(doctrine, "name", "") or "This doctrine")
        return _error(f"{name} has no refits")

    if command.reset:
        changed = sum(1 for m in slots
                      if str(getattr(m, "fit_id", "") or "")
                      != str(slot_refits(m)[0]))
        if not changed:
            return _noop("refits already at defaults")
        return Resolution(kind="reset", message="Refits reset to defaults")

    tokens = list(command.tokens or ())
    if not tokens and command.selector:
        tokens = str(command.selector).split()
    if not tokens:
        return _error("missing refit number or name")

    # The first token is a slot spec only if it actually names a slot; if it
    # does, everything after it is the selector, and there must BE something
    # after it ("refit dps" picked a ship but no fit).
    filtered = _slot_filter(tokens[0], slots, get_fit)
    if not filtered:
        return _resolve_selector(tokens, slots, get_fit)

    primary = _resolve_selector(tokens[1:], filtered, get_fit)
    if primary.kind != "error":
        return primary
    # The slot reading FAILED, so try the line as one whole selector before
    # reporting that failure. Fit names begin with their hull ("Muninn AC"),
    # which means the hull-prefix slot filter eats the first word of the most
    # natural thing an FC types — leaving "ac", which is below the fragment
    # floor. Only a SUCCESS overrides: a filtered error usually says something
    # sharper ("Scimitar has only 2 refits") than the whole-line miss would,
    # and never swapping on a failure is the module's contract.
    if len(tokens) > 1:
        alternative = _resolve_selector(tokens, slots, get_fit)
        if alternative.kind != "error":
            return alternative
    else:
        # A bare slot name ("refit muninn"): offer that slot's refits rather
        # than answering "missing refit number or name" to a line that named
        # exactly one ship.
        alternative = _resolve_fragment(tokens[0], filtered, get_fit)
        if alternative.kind != "error":
            return alternative
    return primary


def _resolve_selector(tokens, candidates, get_fit) -> Resolution:
    """Dispatch a selector — an all-digit pick or a name fragment."""
    if not tokens:
        return _error("missing refit number or name")
    selector = " ".join(tokens)
    if selector.isdigit():
        return _resolve_number(int(selector), candidates, get_fit)
    return _resolve_fragment(selector, candidates, get_fit)


# ── per-sender cooldown ──────────────────────────────────────────────────────

class Cooldown:
    """Per-sender rate gate, shaped like ``range_check.RangeCheckTrigger``.

    Exists so no caller has to remember to stamp the clock by hand — the one
    mistake that turns a cooldown into a no-op and lets a repeated line
    double-swap a doctrine.

    Clock is ``time.monotonic``, never ``time.time``: a wall clock steps
    BACKWARDS under an NTP correction, and a backwards step makes
    ``now - previous`` negative, suppressing the feature for the length of the
    jump. ``now`` stays injectable so the window is testable without sleeping.

    Single-consumer by design: the chat tail is one worker thread, and the
    check-then-stamp in ``consider`` is not atomic across threads."""

    def __init__(self, cooldown_s=COOLDOWN_SECONDS):
        try:
            self.cooldown_s = max(0.0, float(cooldown_s))
        except (TypeError, ValueError):
            self.cooldown_s = COOLDOWN_SECONDS
        self._last: dict[str, float] = {}

    @staticmethod
    def _key(sender_key) -> str:
        return str(sender_key or "").strip().lower()

    def consider(self, sender_key, now=None) -> bool:
        """True (and the cooldown is stamped) iff this sender may fire now."""
        key = self._key(sender_key)
        if not key:
            return False
        try:
            stamp = time.monotonic() if now is None else float(now)
        except (TypeError, ValueError):
            stamp = time.monotonic()
        previous = self._last.get(key)
        if previous is not None and (stamp - previous) < self.cooldown_s:
            return False
        self._last[key] = stamp
        return True

    def last_fired(self, sender_key):
        return self._last.get(self._key(sender_key))

    def reset(self):
        self._last.clear()
