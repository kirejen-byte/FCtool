"""Fleet-chat "range check" — pure trigger/report engine + the over-client toast.

Design: ``docs/superpowers/specs/2026-07-27-range-check-and-channel-search-design.md``
(Feature 2). Typing the keyword in **Fleet** chat pops a short-lived window over
the EVE client that posted it, summarising who can reach that character's system.

Split of responsibility — everything above ``RangeToast`` is **pure**: no Tk, no
network, no threads, no disk. Every outside fact arrives through an injected
callable (``resolve`` for names, ``distance_fn`` for LY distances), so the whole
engine is testable with no display and no coordinate table.

Load-bearing decisions, each one a place this feature can be silently wrong:

* **Only the owner's own logged-in characters trigger it.** The sender gate is
  ``eve_client_tracker.ClientWindow.key`` — ``char_name.strip().lower()``, an
  exact join with the ESI poller's location-state keys — so a fleet member (or
  any relay) saying the keyword can never put a window on the owner's screen.
  It also excludes EVE's own MOTD replay for free: a fleet-join MOTD is posted
  by the sender ``EVE System``, which is nobody's character, so a replayed MOTD
  containing both the keyword and a pile of system names is a silent no-op.
* **A blank keyword disables the feature** rather than matching every line.
  ``normalize_config`` therefore never substitutes the default for an explicitly
  blank keyword — only for an ABSENT one.
* **Ranges come from ``jump_range.JumpRangeChecker.SHIP_RANGES`` — the CLASS
  dict — read live.** Those values are already the JDC-5 (max) ranges: hull base
  x 2.0, i.e. Titan 6.0, Dreadnought 7.0, Command Carrier 7.5, Black Ops 8.0.
  This module deliberately does NOT route through
  ``JumpRangeChecker(custom_ranges=...)``
  and reads NO per-hull range out of config: that config key currently drives
  nothing at all in the app, and a config persisted before v2.8.1 can still
  carry pre-fix values that would silently override a corrected default and
  produce a WRONG range answer. Honouring it here would promote a dead key back
  to live as a side effect of this feature. If per-hull customisation is ever
  wanted it needs a deliberate config migration. (Drift guard:
  ``test_range_check.py::test_the_module_reads_no_per_hull_range_config_key``.)
* **The hull set has ONE definition (``HULL_COLUMNS``) and every per-hull answer
  is keyed off it.** ``RangeRow.reach`` is a frozen mapping built by
  ``reach_map``, never one boolean field per hull: parallel booleans is the
  shape that rots the moment a hull is added, because every producer, consumer
  and test has to grow a field and the one that does not silently answers for
  the wrong hull. ``in_range`` is total — a hull the row has no answer for
  RAISES rather than returning a plausible ``False``. The fallback used when a
  key vanishes from ``SHIP_RANGES`` is per-hull for the same reason: one shared
  7.0 silently UNDER-reported the Black Ops' 8.0, and under-reporting is the
  dangerous direction for a feature answering "who can reach me".
* **Linked systems REPLACE the source list, never the target.** A message naming
  systems is the FC asking "range from THESE", so the configured stagings step
  aside entirely. That makes a FALSE system match expensive rather than
  cosmetic — it deletes every hostile row — so two defences sit either side of
  it: ``plain_phrase_is_a_reference`` refuses letters-only words that read as
  English rather than as a link, and ``provenance_line`` always tells the FC
  which systems the message overrode the sources with.
* **The gate discloses in BOTH directions.** ``provenance_line`` covers what
  the message took; ``ignored_line`` covers what it refused. The gate refuses
  quietly, and quietly is the one thing this module may not be: ``range check
  Jita`` gave a LINKED row while ``range check jita`` gave the default report,
  byte-for-byte the shape of "you named nothing". Measured on the owner's 304k
  Fleet messages the gate refuses 27.3% of mid-sentence real-name mentions
  (mostly lowercase typing). The mirror is advisory and dim — it does NOT
  weaken the gate, and it names refusals no retyping can fix without pretending
  otherwise (see ``_retype_hint``).
* **A name that will not resolve becomes an unresolved ROW, never a dropped
  one.** A missing row reads as "nothing there", which is the dangerous failure
  direction for a feature whose whole job is telling you who can reach you. Same
  for a distance the injected function cannot supply, same for a configured
  staging list the app cannot read (that becomes a visible warning, not
  silence), and same for an unknown target: the report says so instead of
  answering.
* **Allegiance is never guessed.** A linked system is FRIENDLY or HOSTILE only
  by membership in the configured lists; anything else lands in its own LINKED
  group. A system configured into BOTH lists resolves as HOSTILE — the safe
  error direction for a config the owner has contradicted — and the DEFAULT
  source grouping routes through the same ``group_of``, so the answer cannot
  depend on whether the FC happened to name the system in the message.
* **Every configured staging is a SOURCE — both lists and the primary.** The
  friendly list used to do nothing but GROUP a system that entered the report
  by another route; it could not put a row on the board at all, while the
  hostile list from the SAME Settings page contributed one per entry. An owner
  with three friendly stagings configured therefore got a report that silently
  dropped all three, and — with a staging system set — not even
  ``NOTE_NO_FRIENDLY`` fired to explain the absence. That asymmetry was exactly
  the "a missing row reads as nothing there" failure the rest of this module is
  built to prevent, so ``default_sources`` now emits all three (through
  ``group_of``, so a both-listed system still reads HOSTILE) and
  ``build_report`` de-duplicates them in the ONE place that already did.
* **A group that is absent for a CONFIG reason says so.** With no friendly
  source configured AT ALL — no Settings > Staging System *and* an empty
  friendly staging list — a configured hostile list renders hostile rows under
  no FRIENDLY header, which reads exactly like "no friendly staging is in
  range". ``NOTE_NO_FRIENDLY`` is that group's missing-row note.
* **The staging is Settings > Staging System** (``zkillboard.staging_system``),
  system-wide, mirroring ``implant_reminder.resolve_staging``'s rung 2. No
  ``market.*`` key is read here, and neither is the implant reminder's own
  staging override (that override belongs to that feature, not this one).
* Distances are compared RAW and only rounded for display — never compare a
  rounded float (the security-band lesson).
* **A source system that IS the target's system renders a caution, never a
  green in-range row.** A capital cannot jump within its own system, and the
  previews-off fallback attributes the target to the FC's OWN system — so "the
  FC is sitting in staging" is the common case this must get right, not an
  edge case. ``ROW_SAME_SYSTEM`` is detected by ``system_id`` equality BEFORE
  ``distance_fn`` is ever called (so a distance function that answers 0.0 for
  a same-system pair cannot silently undo the fix), carries the real 0.0
  distance rather than suppressing it, and sorts in the same degraded tier as
  an unresolved/no-distance row — it must never rank as the group's nearest,
  best answer the way a genuine 0.0 ly in-range row would. Red/green stay
  reserved for actual out-of-range/in-range verdicts; this is yellow, and
  yellow means caution only.

Threading: the chat tail runs on a worker thread. Everything in the engine is
safe there; ``RangeToast`` is Tk-thread only, so the caller marshals the show
through ``FCToolGUI._post_ui``.
"""
from __future__ import annotations

import bisect
import re
import time
import tkinter as tk
from dataclasses import dataclass
from types import MappingProxyType
from typing import NamedTuple

import jump_range
import system_coords
from app_log import get_logger
from chat_monitor import MESSAGE_PATTERN
from client_toast import (ALPHA, FADE_MS, FADE_STEPS, RETOP_MS, place_over,
                          scaled_bounds)
# The token shape of an EVE system name (letters, digits, dashes) has ONE
# definition in this project — intel_stream's. Reused, never re-typed, so a
# future correction there reaches this parser too.
from intel_stream import _WORD_RE as SYSTEM_TOKEN_RE
from ui_theme import (BG_PANEL, BORDER_COLOR, FG_ACCENT, FG_DIM, FG_GREEN,
                      FG_ORANGE, FG_RED, FG_TEXT, FG_YELLOW)

log = get_logger(__name__)


# ── config ───────────────────────────────────────────────────────────────────

#: Shape + defaults of ``config['range_check']``. Mirrored in
#: ``default_config.DEFAULT_CONFIG`` by the wiring; kept here too so the engine
#: self-heals a partially-written block (house per-key defaulting, never a deep
#: merge). Deliberately only these two keys — the toast hold and the per-sender
#: cooldown are module constants, not config surface.
DEFAULTS = {
    "enabled": True,             # MASTER GATE — ON by default (2026-09): it
                                 # reacts only to the FC's OWN characters and
                                 # draws over a client, same posture as
                                 # `refit_command`/`ozone_watch` (both ON) —
                                 # the owner wants it working out of the box.
                                 # An install where the owner ever pressed
                                 # Save in Settings has an explicit
                                 # `range_check.enabled: false` PERSISTED
                                 # (`_collect_range_check_settings` always
                                 # writes the block) and keeps reading OFF —
                                 # that is correct, not a bug; do not migrate it.
    "keyword": "range check",    # case-insensitive substring; BLANK = disabled
}

#: Per-sender cooldown so a repeated keyword cannot stack popups.
COOLDOWN_SECONDS = 20.0
#: Toast hold before the fade, matching the implant reminder's feel.
TOAST_SECONDS = 10.0


def is_enabled(block) -> bool:
    """Is the range check switched on, given a RAW ``config['range_check']``?

    The single answer to the master-gate question — the Settings checkbox and
    the chat hookup must never re-derive it independently (that is how the
    implant reminder once shipped a ticked box over a feature that could not
    fire). Absent / ``None`` / malformed all inherit ``DEFAULTS['enabled']``
    (True); only an explicit, falsy ``enabled`` turns it off. Never raises.

    An absent or malformed block reads as ARMED: the default is ON, so only
    an explicit ``enabled: false`` disables the range check."""
    if not isinstance(block, dict):
        return bool(DEFAULTS["enabled"])
    return bool(block.get("enabled", DEFAULTS["enabled"]))


def normalize_config(raw) -> dict:
    """Coerce ``config['range_check']`` into a fully-populated, sane dict.

    Never raises and never returns a partial shape. The one rule worth stating:
    an ABSENT keyword inherits the default, an explicitly BLANK one stays blank
    — and a blank keyword disables the feature (see ``should_fire``). Silently
    restoring the default over a keyword the owner deliberately cleared would
    re-arm a feature they switched off with the edit. Pure; the caller's dict is
    not mutated."""
    src = raw if isinstance(raw, dict) else {}
    out = dict(DEFAULTS)
    out["enabled"] = is_enabled(src)
    out["keyword"] = str(src.get("keyword", DEFAULTS["keyword"]) or "").strip()
    return out


# ── chat line parsing ────────────────────────────────────────────────────────

class ChatLine(NamedTuple):
    """One parsed chat line: ``(sender, body)``, unpackable as a plain tuple."""
    sender: str
    body: str
    stamp: str = ""          # the log's own "YYYY.MM.DD HH:MM:SS", diagnostic only


def parse_chat_line(raw) -> ChatLine | None:
    """``"[ 2026.07.28 03:21:04 ] Name > body"`` -> ``ChatLine``; None otherwise.

    Uses ``chat_monitor.MESSAGE_PATTERN`` — the shipped, BOM-tolerant line
    regex — rather than a second copy of it. Header lines, blank lines and
    anything else return None. A caller already holding a ``ChatMessage`` from
    ``ChatMonitor`` does not need this at all: ``(msg.sender, msg.message)`` is
    the same pair."""
    if not isinstance(raw, str):
        return None
    m = MESSAGE_PATTERN.match(raw.rstrip("\r\n"))
    if not m:
        return None
    return ChatLine(sender=m.group(2).strip(), body=m.group(3).strip(),
                    stamp=m.group(1).strip())


# ── the fire gate ────────────────────────────────────────────────────────────

def own_key(name) -> str:
    """``eve_client_tracker.ClientWindow.key`` for a chat sender name — the
    stripped, lowercased character name that joins chat, client windows and the
    ESI poller's location state."""
    return str(name or "").strip().lower()


def _key_set(own_keys) -> frozenset:
    """Normalize whatever the caller passed into a set of comparable keys, so a
    wiring that hands over raw character names still gates correctly."""
    if not own_keys:
        return frozenset()
    return frozenset(k for k in (own_key(x) for x in own_keys) if k)


def matches_keyword(body, keyword) -> bool:
    """Case-insensitive substring match, anywhere in the body. A blank keyword
    matches NOTHING — that is how a cleared keyword disables the feature instead
    of firing on every line."""
    needle = str(keyword or "").strip().lower()
    if not needle:
        return False
    return needle in str(body or "").lower()


def _keyword_tail_start(body, keyword) -> int | None:
    """Offset just PAST the FIRST occurrence of ``keyword`` in ``body``, or
    ``None`` when the keyword is blank or absent.

    Exactly the case-insensitive, anywhere-in-the-line notion of a match
    ``matches_keyword`` uses -- ONE owner for "where does the command end", so
    the tail can never disagree with the gate that fired the report."""
    needle = str(keyword or "").strip().lower()
    if not needle:
        return None
    at = str(body or "").lower().find(needle)
    if at < 0:
        return None
    return at + len(needle)


def keyword_tail(body, keyword) -> str | None:
    """Everything the FC typed AFTER the range-check keyword, or ``None``.

    ``keyword_tail("hey range check svm", "range check")`` -> ``" svm"``. A
    blank keyword, or one the body does not carry, gives ``None`` (not ""), so
    "no command on this line" and "the command with nothing after it" stay
    distinguishable.

    The command TAIL is the one place in a chat line where a bare string is a
    system name by construction: the FC asked for a range check and then named
    the system. ``extract_systems`` takes that as the evidence the general
    prose gates cannot have (owner rule, 2026-09-13)."""
    start = _keyword_tail_start(body, keyword)
    if start is None:
        return None
    return str(body or "")[start:]


def should_fire(sender, body, *, keyword, own_keys, last_fired=None,
                now=None, cooldown_s=COOLDOWN_SECONDS) -> bool:
    """Should this chat line pop a range check? Pure — nothing is mutated.

    All four gates must pass: a non-blank keyword present in the body, a sender
    that is one of the owner's OWN characters, and no fire from that same
    character within ``cooldown_s``. ``last_fired`` is a read-only mapping of
    sender key -> last fire timestamp (same clock as ``now``); the caller stamps
    it (or uses ``RangeCheckTrigger``, which owns the stamping).

    The default clock is ``time.monotonic``, never ``time.time``: a wall clock
    steps BACKWARDS under an NTP correction, and a backwards step makes
    ``now - previous`` negative, which suppresses the feature for the length of
    the jump. Monotonic time cannot go backwards and its epoch is irrelevant
    here because nothing outlives the process. ``now`` stays injectable so the
    cooldown is testable without sleeping."""
    if not matches_keyword(body, keyword):
        return False
    key = own_key(sender)
    if not key or key not in _key_set(own_keys):
        return False
    if not last_fired:
        return True
    try:
        previous = float(last_fired.get(key))
    except (AttributeError, TypeError, ValueError):
        return True
    stamp = time.monotonic() if now is None else float(now)
    try:
        window = float(cooldown_s)
    except (TypeError, ValueError):
        window = COOLDOWN_SECONDS
    return (stamp - previous) >= window


class RangeCheckTrigger:
    """``should_fire`` plus the per-sender clock it needs. Not Tk, not threaded.

    Exists so no caller has to remember to stamp the cooldown by hand — the one
    mistake that turns the cooldown into a no-op and lets a repeated keyword
    stack popups over a client.

    Single-consumer by design: the chat tail is one worker thread, and the
    check-then-stamp in ``consider`` is not atomic across threads."""

    def __init__(self, keyword="", cooldown_s=COOLDOWN_SECONDS):
        self.keyword = str(keyword or "")
        try:
            self.cooldown_s = max(0.0, float(cooldown_s))
        except (TypeError, ValueError):
            self.cooldown_s = COOLDOWN_SECONDS
        self._last_fired: dict[str, float] = {}

    def consider(self, sender, body, own_keys, now=None) -> bool:
        """True (and the cooldown is stamped) iff this line should fire.

        Clock is ``time.monotonic`` — the stamps it writes are only ever
        compared against each other, and a wall clock stepping backwards under
        NTP would suppress the feature for the length of the step."""
        stamp = time.monotonic() if now is None else float(now)
        if not should_fire(sender, body, keyword=self.keyword,
                           own_keys=own_keys, last_fired=self._last_fired,
                           now=stamp, cooldown_s=self.cooldown_s):
            return False
        self._last_fired[own_key(sender)] = stamp
        return True

    def last_fired(self, sender):
        return self._last_fired.get(own_key(sender))

    def reset(self):
        self._last_fired.clear()


# ── system references ────────────────────────────────────────────────────────

@dataclass(frozen=True)
class SystemRef:
    """A system by NAME, with its id when something could resolve it.

    ``system_id is None`` means unresolved, and that is a first-class state:
    such a ref still becomes a visible row (see ``build_report``). Two producers
    share the type — ``extract_systems`` only ever emits resolved refs (an
    unresolvable word in a chat line is not a system reference, it is a word),
    while ``resolve_sources`` emits unresolved refs freely, because a CONFIGURED
    staging name that will not resolve is a real source the owner must be told
    about."""
    name: str
    system_id: int | None = None

    @property
    def resolved(self) -> bool:
        return self.system_id is not None

    @property
    def key(self) -> str:
        return self.name.strip().lower()


def _is_id(value) -> bool:
    """A usable system id: a positive int, and NOT a bool (``True`` is an int)."""
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _resolve_id(name, resolve):
    """``resolve(name)`` hardened to "a positive int, or None".

    ``resolve=None`` means "no resolver was supplied", which is itself an
    answer: unresolved. Every failure mode — a raising resolver, a None, a
    string, a zero — lands on the same unresolved verdict, because an
    unresolved ref is a first-class visible state here, not an error."""
    if resolve is None:
        return None
    try:
        sid = resolve(str(name))
    except Exception:
        return None
    try:
        sid = int(sid)
    except (TypeError, ValueError):
        return None
    return sid if sid > 0 else None


def _table_name(system_id) -> str:
    """``system_coords.get_name`` hardened to "a name, or ``""``".

    The bundled table is shipped data and ``system_coords._load`` already
    swallows a corrupt file, so this is belt-and-braces -- but it is the last
    outside fact this module still read BARE, and the module's whole contract is
    that an outside fact degrades rather than raises. The consequence of a raise
    is invisible rather than loud: ``extract_systems`` runs on the chat worker
    under ``fc_gui._range_check_observe``'s blanket ``except``, so the only
    symptom an FC sees is a toast that never appears."""
    try:
        return system_coords.get_name(system_id) or ""
    except Exception:
        log.debug("range_check: get_name(%r) failed", system_id, exc_info=True)
        return ""


#: FCs type jump COUNTS constantly ("2-3 out", "5-10 min") and no real system
#: name is a bare digit range -- excluded from partial matching outright,
#: whatever its length (2026-08-24 false-positive fix, F2).
_NUMBER_RANGE_RE = re.compile(r"^\d+-\d+$")
#: Below this length a letters-only fragment is ordinary chat, not a typed
#: system name -- "Gate"/"LOST"/"DEAD"/"FAST" are all 4 letters; real
#: abbreviations an FC would actually type run 5+ ("Amama" -> Amamake).
MIN_PARTIAL_LETTERS_LEN = 5
#: A digit/dash-SHAPED fragment (not a pure number range) is eligible for
#: PREFIX matching at 3 characters -- "1dh" -> 1DH-SX, "3-F" -> 3-FKCZ are
#: real, unambiguous FC shorthand for null/lowsec abbreviations, and a prefix
#: match anchors on the START of a name, so a short shaped fragment collides
#: with nothing real when it is prefix-unique (2026-08-25 split floor).
MIN_PARTIAL_SHAPED_PREFIX_LEN = 3
#: SUBSTRING matching for a shaped fragment still needs 4+ characters. A
#: 3-char fragment that only ever matches MID-name is exactly how "8th" ->
#: 09-8TH, "b52" -> 85-B52, "c-2"/"2-a" become false positives, and a false
#: hit REPLACES the whole source list (deleting hostile rows). Prefix-unique
#: short abbreviations are safe; substring-only short fragments are not, so
#: the two stages carry different floors (2026-08-25 split floor).
MIN_PARTIAL_SHAPED_SUBSTR_LEN = 4
#: A LETTERS-only fragment this short is eligible for PREFIX matching too --
#: but only against a SHAPED candidate. An FC abbreviating a null-sec system
#: types the letters that lead it and drops the dash tail ("SVM" for SVM-3K,
#: the owner's own prior staging, field-reported 2026-09-13: the report
#: silently answered about the CURRENT staging instead), and a fragment whose
#: one candidate carries a digit or a dash cannot be the English word it looks
#: like -- ``Gate`` still refuses, because ``Gateway`` is letters-only and the
#: 5-character letters floor above owns that class. The casing gate
#: (``plain_phrase_is_a_reference``, ``MIN_PLAIN_ABBREV_LEN``) is the second
#: half of this: a shaped name is spelled in CAPS, so only a CAPS fragment
#: matches its lead character for character, and prose never does.
MIN_PARTIAL_ABBREV_LEN = 3


class _PrefixCatalogue(dict):
    """The ``{lower_name: original_name}`` catalogue with a sorted key list.

    A plain ``dict`` to every reader -- ``.get``, ``.items()`` and ``in`` are
    untouched -- plus ONE extra operation: ``prefix_hits``, a ``bisect`` over
    the sorted keys instead of a 5,485-entry ``startswith`` scan.

    It exists because both partial-matching stages are prefix scans and both
    run per WORD. Measured on a 2,001-character line with 300 tail tokens
    (``py -3.12``, this box): 900 ``_partial_match`` calls plus 300
    ``tail_candidate`` calls issued **6.58 million** ``str.startswith`` calls
    and the whole extraction took **0.75 s** on the chat worker thread. The
    bisect makes each prefix stage O(log n) and the same line takes **0.18 s**.
    Nothing about the ANSWERS changes -- the helpers below fall back to the
    linear scan for a plain dict, which is what every direct caller (and every
    test that hands one in) passes."""

    __slots__ = ("_keys",)

    def __init__(self, mapping=()):
        super().__init__(mapping)
        self._keys = sorted(self)

    def prefix_hits(self, needle, limit=2) -> list:
        """Up to ``limit`` original names whose lowered form starts with
        ``needle``. ``limit`` is 2 because every caller only ever asks "one, or
        more than one" -- a third candidate changes no answer."""
        keys = self._keys
        i = bisect.bisect_left(keys, needle)
        out = []
        while i < len(keys) and len(out) < limit and keys[i].startswith(needle):
            out.append(self[keys[i]])
            i += 1
        return out


def _prefix_hits(catalogue, needle, limit=2) -> list:
    """``prefix_hits`` off a ``_PrefixCatalogue``, else the linear equivalent.

    ONE definition of "which catalogue names start with this", so the fast and
    the fallback path cannot answer differently -- and so ``tail_candidate(tok,
    {"jita": "Jita"})`` keeps working for every caller holding a plain dict."""
    fast = getattr(catalogue, "prefix_hits", None)
    if fast is not None:
        return fast(needle, limit)
    out = []
    for lower, original in catalogue.items():
        if lower.startswith(needle):
            out.append(original)
            if len(out) >= limit:
                break
    return out


def resolve_partial_name(name, catalogue) -> str | None:
    """Resolve a possibly-PARTIAL, case-insensitive ``name`` against a
    system-name ``catalogue`` -- a ``{lower_name: original_name}`` dict,
    lowered ONCE by the caller (``extract_systems`` builds it once per call,
    never per phrase, so this function never lowers a catalogue entry itself
    -- only the typed name; ``2026-08-24``, fleet-chat partial name matching,
    revised the same day after a false-positive/perf review, F1-F3/F5). For
    the same reason, ``_refused_ref``'s own catalogue lookup (2026-09-13) is
    gated to abbreviation-shaped single words before it ever scans -- a full
    catalogue walk per REFUSED phrase, unbounded, measured at 60-148 ms per
    prose line.
    Returns the ONE catalogue name ``name`` identifies, or ``None``. Any
    non-dict-like catalogue (``None`` included) degrades to ``None`` rather
    than raising -- the same "every failure mode is unresolved" contract
    ``_resolve_id`` keeps.

    An exact hit aside, a partial hit needs real evidence: ordinary chat words
    and jump-count fragments vastly outnumber abbreviated system names in real
    fleet chat, and a false hit REPLACES the whole source list.

      1. EXACT case-insensitive match always wins -- O(1) via the dict --
         whatever the phrase's shape or length; this is the ONLY rule the two
         eligibility gates below do not apply to.
      2. A pure jump-count shape (``_NUMBER_RANGE_RE`` -- "2-3", "5-10",
         "0-2") NEVER partial-matches.
      3. A LETTERS-ONLY phrase (no digit, no dash) needs length >=
         ``MIN_PARTIAL_LETTERS_LEN`` and gets PREFIX matching only -- never
         substring (kills "Gate"/"LOST"/"DEAD"/"FAST" while still catching
         "Amama" -> "Amamake"). BELOW that floor it still gets one PREFIX
         chance, down to ``MIN_PARTIAL_ABBREV_LEN``, but ONLY if the single
         candidate is itself SHAPED: "SVM" reaches SVM-3K, "Gate" still
         cannot reach Gateway (2026-09-13, the owner's "range check SVM"
         answering about the current staging instead).
      4. A SHAPED phrase (digit or dash present, not a pure jump-count) gets
         PREFIX matching at length >= ``MIN_PARTIAL_SHAPED_PREFIX_LEN`` (3, so
         "1dh" -> 1DH-SX resolves), then -- only if prefix found NOTHING --
         SUBSTRING matching, which needs the stricter length >=
         ``MIN_PARTIAL_SHAPED_SUBSTR_LEN`` (4, so a 3-char fragment matched
         mid-name -- "8th"/"b52"/"c-2" -- can never fire). Both stages require
         a UNIQUE candidate; no fuzzy tie-break.

    Single pass over the catalogue for the prefix/substring stages (no
    repeated ``.lower()``, no separate substring pass when prefix already
    decided the answer); bails out as soon as the prefix count is provably
    ambiguous, since nothing later in the scan can undo that."""
    return _partial_match(name, catalogue)[0]


def _partial_match(name, catalogue) -> tuple:
    """``(resolved_name | None, ambiguous)`` -- the scan itself, carrying the
    one fact ``resolve_partial_name``'s ``str | None`` return cannot: whether
    the fragment matched SEVERAL real systems.

    That distinction is a DISCLOSURE, not a resolution. An ambiguous fragment
    ("P-Z" over P-ZMZV and P-ZWKH) means the FC named something real and the
    report is about to fall back to the configured stagings regardless -- the
    silence ``ignored_line`` exists to break. A fragment that matched NOTHING
    is not a disclosure: this module does not get to call an unknown word a
    system. Ambiguity is reported for the stage that actually decided the
    answer -- a prefix that hit twice, or (prefix empty) a substring that did
    -- and never for a fragment refused by a length/shape gate before the
    scan, which named nothing at all."""
    text = str(name or "").strip()
    if not text:
        return None, False
    try:
        exact = catalogue.get(text.lower())
    except AttributeError:
        return None, False
    if exact is not None:
        return exact, False
    if _NUMBER_RANGE_RE.match(text):
        return None, False
    shaped = is_system_shaped(text)
    # The early gate uses the PREFIX floor -- a 3-char shaped phrase is still
    # eligible to enter the scan (for prefix matching only, per substr_ok), and
    # so is a 3-char letters-only ABBREVIATION (for a shaped candidate only,
    # per abbrev_only).
    if len(text) < (MIN_PARTIAL_SHAPED_PREFIX_LEN if shaped
                    else MIN_PARTIAL_ABBREV_LEN):
        return None, False
    # A letters-only fragment under the letters floor is an FC's shorthand for
    # a SHAPED name or it is nothing: prefix only, and the one candidate must
    # carry a digit or a dash. Keeps "Gate" -> Gateway refused while "SVM"
    # reaches SVM-3K.
    abbrev_only = not shaped and len(text) < MIN_PARTIAL_LETTERS_LEN
    needle = text.lower()
    # Substring matching carries the STRICTER shaped floor, so a 3-char shaped
    # phrase does prefix-matching only and never substring-matches mid-name.
    substr_ok = shaped and len(text) >= MIN_PARTIAL_SHAPED_SUBSTR_LEN
    prefix = _prefix_hits(catalogue, needle)
    if len(prefix) == 1:
        if abbrev_only and not is_system_shaped(prefix[0]):
            return None, False
        return prefix[0], False
    if prefix:
        return None, True           # 2+ prefix candidates: really ambiguous
    if not (shaped and substr_ok):
        return None, False
    # Only reached when the prefix stage found NOTHING, so no entry here can
    # start with the needle and a bare ``in`` test is exactly the old ``elif``.
    substr_hits = 0
    substr_cand = None
    for lower, original in catalogue.items():
        if needle in lower:
            substr_hits += 1
            substr_cand = original
            if substr_hits > 1:
                break            # 2+ is 2+; nothing later can undo it
    if substr_hits == 1:
        return substr_cand, False
    return None, substr_hits > 1


#: Punctuation an FC wraps around a name in the command tail. ``SYSTEM_TOKEN_RE``
#: already refuses to take commas, quotes and brackets INTO a token, so this is
#: a belt-and-braces trim of a leading/trailing dash ("svm-" typed mid-thought)
#: -- internal dashes are load-bearing and are never touched.
_TAIL_STRIP = "-'\"`.,;:!?()[]{}<>"


def tail_token(word) -> str:
    """One command-tail word, stripped of the punctuation around it."""
    return str(word or "").strip().strip(_TAIL_STRIP)


def tail_candidate(token, catalogue) -> tuple:
    """``(resolved_name | None, ambiguous)`` for ONE command-tail token.

    The owner's rule, 2026-09-13, and the whole reason it can be this simple:
    **after the range-check keyword there is no prose to protect against.** The
    FC typed the command and then typed the system, so the length/casing/
    position evidence ``plain_phrase_is_a_reference`` exists to weigh is not
    needed here -- ``svm``, ``Svm``, ``SVM``, ``svm-3``, ``p-zm`` and ``gatew``
    all name SVM-3K / P-ZMZV / Gateway, whatever their length or casing, iff
    they name exactly ONE system.

      1. EXACT case-insensitive catalogue hit wins, as everywhere else.
      2. Otherwise a case-insensitive PREFIX scan: exactly ONE candidate is the
         answer, two or more is ``ambiguous`` (a DISCLOSURE, never a
         resolution -- see ``_partial_match``), none is silence. An unknown
         word is still not a system: ``range check the fleet`` must name
         nothing rather than invent something.
      3. A pure jump-count shape ("2-3", "5-10") or a bare count ("10", "5")
         never PREFIX-matches, the one gate kept from the general path: FCs
         type counts constantly and a false link REPLACES the whole source
         list -- ``range check 10`` must not silently become 10UZ-P. An exact
         name of either shape would still resolve at step 1.

    O(catalogue) per tail token, bailing out the moment the prefix count is
    provably ambiguous -- the tail is a handful of words and the catalogue is
    built ONCE per call, so this never becomes the per-window scan the general
    path was measured (and gated) for."""
    text = str(token or "").strip()
    if not text:
        return None, False
    try:
        exact = catalogue.get(text.lower())
    except AttributeError:
        return None, False
    if exact is not None:
        return exact, False
    if _NUMBER_RANGE_RE.match(text) or text.isdigit():
        return None, False
    hits = _prefix_hits(catalogue, text.lower())
    if len(hits) == 1:
        return hits[0], False
    return (None, True) if hits else (None, False)


def tail_disclosable(token, *, lone) -> bool:
    """Is an UNRESOLVED command-tail token worth naming on the report?

    The owner's field bug, stated exactly (2026-09-14): ``range check SMV`` --
    one character off SVM-3K -- matched nothing, said nothing, and produced the
    configured-staging report. With the FC sitting in staging that read "same
    system - cannot jump within a system" about a system they never named, and
    nothing anywhere on the toast said the word they typed had been dropped.
    Falling back to the configured sources is fine; falling back WITHOUT A WORD
    on a line whose whole point was a system name is the silence this module
    exists to refuse.

    Two shapes earn the disclosure, and nothing else does:

    * **system-SHAPED** (a digit or a dash, ``MIN_PARTIAL_SHAPED_PREFIX_LEN``+
      characters): ``P-ZMVZ``, ``svn-3k``. No English word looks like that, so
      it was typed as a name whatever else is on the line.
    * **the ONLY token after the keyword**: ``range check SMV``. An FC who typed
      the command and then exactly one word typed that word as the system. A
      token in a CROWD of tail words is not that -- ``range check the fleet`` is
      prose, and "ignored in message: the" would be a different lie (the
      disclosure's own standing rule, see ``extract_systems``).

    Numbers are excluded on both paths. FCs type jump counts constantly
    ("2-3 out", "5 jumps", "10") and a count is shaped by construction, so
    without this a routine ``range check 2-3 jumps out`` would grow a permanent
    "2-3 (not a known system)" tail. An exact system NAME of that shape never
    reaches here -- it resolved.

    The residual, named rather than hidden: a lone prose word (``range check
    now``) is disclosed as "not a known system". That is the price of the lone
    rule and it is the cheap direction -- one dim advisory line about a word the
    FC did type, versus the silent wrong-system answer it replaces."""
    text = str(token or "").strip()
    if not text:
        return False
    if text.isdigit() or _NUMBER_RANGE_RE.match(text):
        return False
    if lone:
        return True
    return (is_system_shaped(text)
            and len(text) >= MIN_PARTIAL_SHAPED_PREFIX_LEN)


def _as_ref(value, resolve=None) -> SystemRef | None:
    """Accept a ``SystemRef``, a bare system id, a NAME, or a ``(name, id)`` pair.

    The bare-id form names itself off the bundled offline table
    (``system_coords.get_name``) so a caller holding only the ESI poller's
    ``solar_system_id`` still gets "Range to J5A-IX" rather than a raw number.
    That is an in-memory read of shipped data — never a network call.

    A NAME string is resolved the same offline way (``resolve``, default
    ``system_coords.resolve_name``). It used to become a permanently unresolved
    ref, so ``build_report("Jita", ...)`` reported "location unknown" forever —
    a silent, permanent degradation a mis-wired caller had no way to notice.
    Silent degradation is the one wrong answer for this module; resolving is the
    honest fix, and it costs a dict lookup.

    A ``SystemRef`` still passes through UNTOUCHED, unresolved id and all: that
    is how the engine represents a name it already tried and could not place
    (``resolve_sources``), and re-resolving it here would paper over exactly the
    state the unresolved row exists to show."""
    if value is None:
        return None
    if isinstance(value, SystemRef):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        if not _is_id(value):
            return None
        return SystemRef(_table_name(value), value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        resolver = system_coords.resolve_name if resolve is None else resolve
        return SystemRef(text, _resolve_id(text, resolver))
    try:
        name, sid = value
    except (TypeError, ValueError):
        return None
    return SystemRef(str(name or "").strip(), sid if _is_id(sid) else None)


def _same_system(a: SystemRef | None, b: SystemRef | None) -> bool:
    """Identity by id when both resolved, else by lowercased name."""
    if a is None or b is None:
        return False
    if a.resolved and b.resolved:
        return a.system_id == b.system_id
    return bool(a.key) and a.key == b.key


#: A phrase carrying a DIGIT or a DASH is system-SHAPED: ``1DH-SX``, ``SVM-3K``,
#: ``J5A-IX``, ``4-2UXV``. No English word looks like that, so such a phrase
#: needs no further evidence that it was meant as a system.
_SYSTEM_SHAPED_RE = re.compile(r"[0-9-]")
#: Shortest letters-only phrase taken out of a chat line as a system reference.
#: 44 real systems have three-letter letters-only names (``Cat``, ``Ham``,
#: ``Jan``, ``Mod``, ``Usi``, ``Hek``) and three-letter English words outnumber
#: them by orders of magnitude — ``Jan`` alone was 1,748 of 21,008 matches over
#: the owner's 304k-message Fleet history, every one of them a date.
MIN_PLAIN_NAME_LEN = 4
#: The ONE thing allowed under ``MIN_PLAIN_NAME_LEN``: an ALL-CAPS letters-only
#: fragment of 3+ characters, the shape of an FC abbreviating a null-sec system
#: ("SVM" for SVM-3K). It buys nothing on its own — a three-letter letters-only
#: NAME is still refused in every casing, because the exact-spelling rule below
#: then demands ``Hek``/``Jan``/``Usi`` and an all-caps phrase is not that. It
#: only lets the fragment reach ``resolve_partial_name``, where
#: ``MIN_PARTIAL_ABBREV_LEN`` requires the one candidate to be SHAPED, and the
#: prefix-casing rule below then requires the fragment to match that candidate's
#: lead CHARACTER FOR CHARACTER — which a shaped (upper-case) name grants only
#: to an upper-case fragment. Prose, whatever its length, never gets there.
MIN_PLAIN_ABBREV_LEN = 3
#: What ends a sentence, so English capitalises the NEXT word whatever it is.
_SENTENCE_END_RE = re.compile(r"[.!?\n\r]")


def is_system_shaped(phrase) -> bool:
    """Does the phrase carry a digit or a dash — the shape of a null/lowsec
    system name? Those are accepted on sight; nothing in English collides."""
    return bool(_SYSTEM_SHAPED_RE.search(str(phrase or "")))


def is_abbrev_shaped(phrase) -> bool:
    """Is this the shape of an FC's typed ABBREVIATION — ALL CAPS, letters
    only, ``MIN_PLAIN_ABBREV_LEN``+ characters ("SVM", "PZM")?

    The only evidence that gets a phrase under ``MIN_PLAIN_NAME_LEN``. It is
    weak on its own and is never used on its own: see that constant."""
    text = str(phrase or "")
    return (len(text) >= MIN_PLAIN_ABBREV_LEN and text.isupper()
            and text.isalpha())


def plain_phrase_is_a_reference(phrase, *, sentence_initial,
                                canonical=None) -> bool:
    """Was this letters-only phrase written as a SYSTEM, or is it just a word?

    1,952 systems have letters-only names and plenty of them are ordinary
    English: ``Toon``, ``Exit``, ``Half``, ``Odin``, ``Usi``, ``Reset``,
    ``Jan``, ``Mod``, ``Access``, and the phrase ``Dead End``, are all real
    systems. Taking one out of a chat line is not a cosmetic error — a linked
    system REPLACES the source list, so ``"range check on my toon"`` collapses
    the whole report to a single LINKED row and every HOSTILE row disappears.

    Three pieces of evidence, none of them a word list to maintain:

    * **Length** — below ``MIN_PLAIN_NAME_LEN`` the English words swamp the
      systems (the measurement is on that constant). The single exception is
      an ALL-CAPS ``is_abbrev_shaped`` fragment, which still has to clear every
      rule below — and does so only as the CAPS lead of a shaped name it
      abbreviates, never as a name in its own right (``MIN_PLAIN_ABBREV_LEN``).
    * **Casing** — a system pasted from the game, or typed as the proper noun it
      is, carries the game's own spelling; chat prose does not (``toon`` vs
      ``Toon``). An all-lowercase phrase is prose. When the bundled table can
      spell the name (``canonical``), the match must be EXACT, so a shouted
      ``EXIT`` is refused as well as a muttered ``exit``.
    * **Position** — English capitalises the first word of a sentence whatever
      it is, so at a sentence start the casing evidence above is worth nothing
      and the phrase is refused rather than believed on no evidence.

    System-SHAPED phrases never come here — they are accepted on sight, which is
    why ``1DH-SX``, ``svm-3k`` and a line-leading ``4-2UXV`` all still work.

    Deliberately imperfect: a mid-sentence ``Toon`` still reads as the system.
    Perfect separation would need a dictionary, and the backstop is cheaper and
    more honest — ``provenance_line`` always names the systems the message
    overrode the sources with, so a surprising report explains itself.

    A ``canonical`` LONGER than ``phrase`` (never possible before partial
    matching) means a letters-only PARTIAL hit is being checked — the exact-
    equality rule above cannot fire (the two strings differ in length by
    construction), so it would silently wave a wrongly-cased fragment through.
    The same discipline applies instead: the typed fragment's casing must
    match the corresponding lead of the canonical name CHARACTER FOR
    CHARACTER, so a shouted ``GATEW`` is refused exactly as a shouted exact
    ``EXIT`` is, while ``Amama`` still reaches ``Amamake`` (2026-08-24, F4)."""
    text = str(phrase or "")
    if len(text) < MIN_PLAIN_NAME_LEN and not is_abbrev_shaped(text):
        return False
    if text == text.lower():
        return False
    if sentence_initial:
        return False
    canon = str(canonical or "")
    if canon and canon.lower() == text.lower():
        return text == canon
    if canon and canon.lower().startswith(text.lower()):
        return canon.startswith(text)
    return True


#: Why a COMMAND-TAIL token was named but not used, rendered by
#: ``ignored_line`` in place of the (impossible) retype hint. Plain ASCII: every
#: note in this module can reach ``log.*`` and this box's console is cp1252.
#: "SMV" alone on the ignored line reads as a system the report dropped; "SMV
#: (not a known system)" is what actually happened, and it is the difference
#: between "retype it" and "the report answered about something else".
REASON_UNKNOWN = "not a known system"
REASON_AMBIGUOUS = "several systems match"


@dataclass(frozen=True)
class IgnoredRef:
    """A letters-only phrase the gate REFUSED that names a real system anyway.

    The mirror image of a ``SystemRef`` out of ``extract_systems``: same sweep,
    opposite verdict. ``retype`` is the spelling that WOULD have been taken, or
    "" when no retyping helps at all — a three-letter name is refused however it
    is spelled and so is a sentence-initial one, so advising a fix there would
    be a lie, and this module does not get to lie about its own rules.

    ``reason`` is the OTHER half of that honesty, for the entries that name no
    system at all (``system_id is None``): a command-tail token that matched
    NOTHING (``REASON_UNKNOWN``) or SEVERAL (``REASON_AMBIGUOUS``). Defaulted
    and last, so every existing positional construction is untouched."""
    phrase: str
    system_id: int | None = None
    retype: str = ""
    reason: str = ""


class SystemMentions(list):
    """The systems ``extract_systems`` TOOK, carrying what it refused.

    A plain ``list`` of ``SystemRef`` for every existing caller and every
    ``== []`` comparison, plus ``.ignored``: the refused-but-real phrases from
    the SAME sweep. They travel together deliberately. Accepted and refused are
    two views of one scan, and ``build_report`` reads ``.ignored`` off the list
    it is already handed — so a wiring that reports which systems it USED cannot
    quietly stop reporting which ones it DROPPED, and the two halves can never
    end up computed from different chat lines. A caller who slices or copies the
    list gets a plain list and loses the tail; pass ``ignored=`` explicitly if
    you need to."""

    def __init__(self, systems=(), ignored=()):
        super().__init__(systems)
        self.ignored = tuple(ignored)


def _retype_hint(phrase, canonical, *, sentence_initial) -> str:
    """The spelling that would have been TAKEN, or "" when none would be.

    The honesty gate on the advice half of the ignored line. ``jan`` is refused
    for LENGTH, so "type it as Jan" would send the FC round a loop that cannot
    terminate; a sentence-initial word is refused whatever its casing, for the
    same reason. Only a difference the gate would actually accept is worth
    suggesting — everything else is named without advice."""
    text = str(phrase or "")
    canon = str(canonical or "")
    if not canon or canon == text:
        return ""
    # A SHAPED canonical never faces this gate at all — a digit or a dash is
    # accepted on sight, in any casing, at any position — so "type it as
    # SVM-3K" is advice that works even where the refused ABBREVIATION could
    # not be fixed by retyping (line-leading, 2026-09-13). Only a partial hit
    # can produce a shaped canonical for a refused plain phrase, which is why
    # this could not arise before.
    if is_system_shaped(canon):
        return canon
    if not plain_phrase_is_a_reference(canon, sentence_initial=sentence_initial,
                                       canonical=canon):
        return ""
    return canon


def _refused_ref(phrase, *, sentence_initial, catalogue=None) -> IgnoredRef | None:
    """``IgnoredRef`` for a refused phrase the BUNDLED table names; else None.

    Deliberately ``system_coords.resolve_name`` and never the injected resolver.
    The gate's whole point is that an injected (possibly ESI-backed) resolver is
    never asked about an English word, and buying the disclosure at the price of
    that guarantee would be a bad trade — the bundled table is an in-memory read
    of shipped data, so the mirror costs a dict lookup and the guarantee stands
    (``test_the_gate_runs_before_the_resolver_is_ever_asked``).

    The consequence, stated so nobody has to rediscover it: a caller injecting a
    resolver that knows MORE systems than the bundled table gets the accepted
    half from their resolver and the refused half from the table. Under-
    disclosure, which is the safe direction for a dim advisory line.

    ``catalogue`` (the caller's ``{lower: original}`` K-space dict) extends the
    mirror to PARTIAL spellings: ``range check svm`` is refused for casing and
    names no system the table can look up EXACTLY, so before 2026-09-13 it
    produced the configured-sources report with no tell at all — the very shape
    the accepted half learned to resolve that day. The candidate is found the
    same way the accepted half finds it, so the two halves cannot disagree
    about what ``svm`` meant, and the retype hint is the spelling that WOULD
    have been taken (``SVM-3K``, shaped, so it bypasses this gate entirely)."""
    sid = _resolve_id(phrase, system_coords.resolve_name)
    canon = ""
    if sid is None:
        if not catalogue:
            return None
        # The catalogue scan is O(catalogue) per call, so it only runs for a
        # phrase actually SHAPED like a miscased abbreviation -- one word, no
        # space, letters only, 3-4 characters. Without this gate every refused
        # phrase on a line paid the full 5,485-entry scan (up to 3 windows per
        # word position), measured at 60 ms for a 41-word prose line and
        # 148 ms for a 92-word one -- the disclosure is not worth that on
        # ordinary chat text that was never an abbreviation to begin with.
        text = str(phrase or "")
        if not (" " not in text and text.isalpha()
                and MIN_PARTIAL_ABBREV_LEN <= len(text) <= MIN_PLAIN_NAME_LEN):
            return None
        candidate = _partial_match(phrase, catalogue)[0]
        if candidate is None:
            return None
        sid = _resolve_id(candidate, system_coords.resolve_name)
        if sid is None:
            return None
        canon = candidate
    canon = canon or _table_name(sid)
    return IgnoredRef(str(phrase), sid,
                      _retype_hint(phrase, canon,
                                   sentence_initial=sentence_initial))


def extract_systems(body, resolve=None, keyword=None) -> SystemMentions:
    """System names mentioned in a chat body, in order of first appearance.

    Same shape as the shipped intel detection (``intel_stream._system_spans``):
    slide 1->3-word windows across the line and take a window as a system iff
    the resolver names it, longest window first, single-word matches under 3
    characters ignored. The resolver is INJECTED (default
    ``system_coords.resolve_name``, the bundled offline table) so this stays
    pure and so a caller can supply an ESI-backed one; it is never a novel fuzzy
    matcher, because a fuzzy hit here would silently answer about the wrong
    system.

    One thing this does NOT share with the intel stream: a letters-only window
    must additionally pass ``plain_phrase_is_a_reference``. The intel stream
    highlights a word it got wrong; here a wrong word REPLACES the source list
    and deletes every hostile row, so the two features cannot afford the same
    false-positive rate. The extra gate is applied BEFORE the resolver wherever
    it can be (length, casing, position need no lookup) so an injected
    ESI-backed resolver is not asked about every English word on the line.

    A linked system renders in the log as its BARE NAME (verified against the
    owner's real Fleet logs, 2026-07-27) — there is no markup to strip. Results
    are de-duplicated by system id.

    **The refusals come back too**, on ``.ignored`` of the returned
    ``SystemMentions``. The gate above refuses QUIETLY, and measured against the
    owner's 304k-message Fleet history it refuses 27.3% of mid-sentence real-name
    mentions (mostly lowercase typing) — so ``range check jita`` produced a report
    byte-for-byte the shape of "you named nothing", with no tell whatsoever. That
    is the silence this module is not allowed to give, in the one direction it was
    still giving it. ``ignored_line`` renders the mirror; see ``_refused_ref`` for
    why the disclosure never costs the injected resolver its guarantee.

    **A PARTIALLY typed name is a second chance, not a second gate**
    (2026-08-24, revised same day after a false-positive/perf review — see
    ``resolve_partial_name``): when the exact resolve above misses, that
    function is tried against the bundled K-space catalogue — length- and
    shape-gated, so ordinary chat words and jump-count fragments ("please",
    "LOST", "2-3") never even reach a catalogue scan — and a hit is
    re-resolved through the SAME injected ``resolver``, never used directly
    for the id. A LETTERS-ONLY partial additionally re-runs the plain-phrase
    casing gate against the ORIGINAL typed text (not the resolved spelling —
    rewriting first would make the gate trivially pass), so a shouted
    fragment is refused exactly as a shouted exact name is. ``"3-FK"`` links
    "3-FKCZ" when it is the only K-space name starting with it; an ambiguous
    or ineligible fragment falls through exactly like an unknown one does
    today; a name that resolves EXACTLY is never second-guessed even when it
    is also a prefix of others.

    **A short ALL-CAPS abbreviation is a partial too** (2026-09-13): the owner
    typed ``range check SVM`` for SVM-3K and got the configured-staging report
    — which, with the FC sitting in that staging, read "same system - cannot
    jump within a system" about a system they had not named. Three characters
    now reach the partial matcher (``MIN_PLAIN_ABBREV_LEN``) and resolve there
    only against a SHAPED candidate (``MIN_PARTIAL_ABBREV_LEN``), so the
    fragment must be the CAPS lead of a null-sec name. Both halves of the
    silence are covered: a MISCASED partial (``svm``) is disclosed by
    ``_refused_ref`` off the same catalogue, and an AMBIGUOUS one (``P-Z`` over
    P-ZMZV and P-ZWKH) becomes an id-less ``IgnoredRef`` — falling back to the
    configured sources without a word, on a line that named real systems, is
    the one answer this module may not give. A fragment matching NOTHING stays
    silent: an unknown word is not a system, and calling it "ignored" would be
    a different lie.

    **After the keyword, a bare string IS a system name** (owner rule,
    2026-09-13, overruling the ALL-CAPS shape above as the general answer):
    "If there is a range check command before a string, it should be attempted
    to be matched to a system name. If there is only one, it should match
    regardless of how long the string is." Pass the configured ``keyword`` and
    every whitespace-separated token AFTER its first occurrence gets that
    second chance -- exact name first, then a unique case-insensitive PREFIX
    over the K-space catalogue (``tail_candidate``), with no length, casing or
    position gate at all: ``range check svm`` / ``Svm`` / ``SVM`` / ``svm-3``
    reach SVM-3K and ``range check gatew`` reaches Gateway.

    **A tail token that resolves to nothing is DISCLOSED, not dropped**
    (2026-09-14, the owner's edge-case pass). ``range check SMV`` -- one
    character off SVM-3K -- used to match nothing, say nothing, and hand back
    the configured-staging report; with the FC sitting in that staging it read
    "same system - cannot jump within a system" about a system they had not
    named. Two shapes of unresolved token now become an id-less ``IgnoredRef``
    carrying a ``reason``, exactly as an ambiguous one already did: a
    system-SHAPED token (``P-ZMVZ``, ``svn-3k``) anywhere in the tail, and ANY
    token when it is the ONLY word after the keyword (``SMV``). The rule and
    its residual live on ``tail_disclosable``; the short version is that a lone
    word after the command was typed as a system, while a word in a crowd
    ("range check the fleet") is prose and still says nothing -- and a jump
    count ("2-3", "10") says nothing on either path. Ambiguity is disclosed on
    the same two shapes rather than guessed (``P-Z`` over P-ZMZV and P-ZWKH).

    The ALL-CAPS abbreviation rule above is what still covers text BEFORE the
    keyword and every ``keyword=None`` caller, so omitting the argument is
    byte-for-byte the pre-2026-09-13-tail behaviour."""
    text = str(body or "")
    resolver = system_coords.resolve_name if resolve is None else resolve
    # Where the COMMAND TAIL begins, or None when the caller named no keyword
    # (or the body does not carry it) -- in which case nothing below changes
    # and this function behaves exactly as it did before the tail rule.
    tail_start = _keyword_tail_start(text, keyword)

    def _sentence_initial(index):
        """Does English force a capital on ``words[index]`` regardless of what
        it is? True at the start of the line and after ``. ! ?``."""
        if index <= 0:
            return True
        return bool(_SENTENCE_END_RE.search(
            text[words[index - 1][2]:words[index][1]]))

    words = [(m.group(0), m.start(), m.end())
             for m in SYSTEM_TOKEN_RE.finditer(text)]
    # Exactly ONE word after the keyword? Then that word IS the system the FC
    # named, and an unresolved one is disclosed rather than dropped in silence
    # (``tail_disclosable``). Counted once, from the same word list the sweep
    # walks, so the two can never disagree about what a "token" is.
    lone_tail = (tail_start is not None
                 and sum(1 for _w, s, _e in words if s >= tail_start) == 1)
    out: list[SystemRef] = []
    ignored: list[IgnoredRef] = []
    seen: set[int] = set()
    refused: set = set()
    catalogue = None            # lazy: only built if an exact match ever fails

    def _catalogue():
        """The ``{lower: original}`` K-space catalogue, built ONCE per call (F3)
        and shared by both halves of the sweep — the accepted one resolving a
        partial spelling and the refused one disclosing it. Lazily, because a
        line whose every phrase resolves exactly never needs it."""
        nonlocal catalogue
        if catalogue is None:
            try:
                names = system_coords.get_kspace_name_to_id()
            except Exception:
                # An unreadable bundled table degrades to "no partial and no
                # command-tail matching" -- exact resolution through the
                # INJECTED resolver still works, so the report still answers.
                # A raise here would escape into the chat worker, where
                # fc_gui's blanket ``except`` turns it into a toast that simply
                # never appears: the invisible failure, not the loud one.
                log.warning("range_check: K-space catalogue unavailable - "
                            "partial and command-tail matching disabled",
                            exc_info=True)
                names = ()
            catalogue = _PrefixCatalogue({nm.lower(): nm for nm in names})
        return catalogue

    i, n = 0, len(words)
    while i < n:
        hit = None
        miss = None                 # longest refused-but-real phrase here
        for size in (3, 2, 1):
            if i + size > n:
                continue
            phrase = " ".join(w for w, _s, _e in words[i:i + size])
            if size == 1 and len(phrase) < 3:
                continue
            plain = not is_system_shaped(phrase)
            initial = _sentence_initial(i)
            # Cheap half of the plain-phrase gate first: length, casing and
            # position are decidable with no lookup at all.
            if plain and not plain_phrase_is_a_reference(
                    phrase, sentence_initial=initial):
                if miss is None:
                    miss = _refused_ref(phrase, sentence_initial=initial,
                                        catalogue=_catalogue())
                continue
            sid = _resolve_id(phrase, resolver)
            partial_name = None         # set only by a successful partial hit
            ambiguous = False           # ...and by a fragment naming SEVERAL
            if sid is None:
                # A typed name that does not resolve EXACTLY gets one more
                # chance: a length/shape-eligible, unique prefix/substring hit
                # against the bundled catalogue is re-resolved through the
                # SAME injected resolver (never used directly for the id,
                # matching ``_refused_ref``'s precedent), so a raising/unaware
                # resolver still yields no match -- only the CANDIDATE
                # spelling comes from the table. Lowered ONCE per call,
                # not per phrase -- and the ENCLOSING ``extract_systems``
                # call itself happens only once per range-check TRIGGER
                # (the fc_gui call site gates on keyword + own-char +
                # cooldown before ever calling it), never per ordinary
                # chat line (see ``resolve_partial_name``, F3).
                partial, ambiguous = _partial_match(phrase, _catalogue())
                if partial is not None and partial.lower() != phrase.lower():
                    partial_sid = _resolve_id(partial, resolver)
                    if partial_sid is not None:
                        sid = partial_sid
                        partial_name = partial
            if sid is None:
                # An AMBIGUOUS abbreviation named real systems and this could
                # not tell which ("P-Z" over P-ZMZV and P-ZWKH). Dropping it
                # silently hands back the configured-sources report — the exact
                # shape of "you named nothing" — for a line that named plenty.
                # Single tokens only: a 2/3-word window that half-matches is
                # not something the FC typed as a name. No id, because there is
                # no ONE system to claim, and no retype hint, because only the
                # FC knows which they meant.
                if ambiguous and size == 1 and miss is None:
                    miss = IgnoredRef(phrase, None, "", REASON_AMBIGUOUS)
                continue
            canon = _table_name(sid)
            # ...and the half that needs the id: the game's own spelling.
            # Runs against the ORIGINAL typed ``phrase`` even for a partial
            # hit (``partial_name or canon`` as the canonical) -- rewriting
            # ``phrase`` to the resolved spelling BEFORE this check would make
            # it trivially pass (F4: text == canon by construction).
            if plain and not plain_phrase_is_a_reference(
                    phrase, sentence_initial=initial,
                    canonical=partial_name or canon):
                if miss is None:
                    miss = IgnoredRef(phrase, sid, _retype_hint(
                        phrase, partial_name or canon,
                        sentence_initial=initial))
                continue
            if partial_name is not None:
                phrase = partial_name   # safe now -- the gate already ran
            hit = (phrase, sid, size, canon)
            break
        if hit is None and tail_start is not None and words[i][1] >= tail_start:
            # The COMMAND TAIL (owner rule, 2026-09-13). Nothing the general
            # sweep recognised sits at this position, and this word came after
            # the keyword -- so the FC typed it as the system they want checked.
            # No length/casing/position gate: a unique prefix IS the answer.
            # Any accepted token clears ``miss`` with it, and the id-dedupe at
            # the end of this function drops a disclosure the tail resolved.
            token = tail_token(words[i][0])
            cand, tail_ambiguous = tail_candidate(token, _catalogue())
            if cand is not None:
                # Re-resolved through the INJECTED resolver, never taken from
                # the table directly -- the same contract the partial path
                # keeps, so an unaware or raising resolver still yields no
                # match and only the CANDIDATE spelling comes from the table.
                tail_sid = _resolve_id(cand, resolver)
                if tail_sid is not None:
                    hit = (cand, tail_sid, 1, _table_name(tail_sid))
            elif miss is None and tail_disclosable(token, lone=lone_tail):
                # The token named SEVERAL systems ("P-Z" over P-ZMZV and
                # P-ZWKH) or NONE at all ("SMV", one character off SVM-3K) --
                # and either way, falling back to the configured stagings
                # without a word is the one answer this module may not give.
                # ``tail_disclosable`` owns which tokens earn the line: filler
                # in a crowd of tail words ("to", "the") and jump counts
                # ("2-3") still say nothing. No id (there is no ONE system to
                # claim) and no retype hint (only the FC knows what they meant)
                # -- the ``reason`` carries what actually happened instead.
                miss = IgnoredRef(token, None, "", REASON_AMBIGUOUS
                                  if tail_ambiguous else REASON_UNKNOWN)
        if hit is None:
            # Nothing was taken at this index, so a refusal here is a real drop
            # and the FC gets told. A refusal UNDER an accepted shorter window
            # is not a drop — something from this position was used.
            # De-duplicated by system id, or — for an ambiguous fragment, which
            # HAS no id — by its own spelling, so one such fragment cannot
            # swallow the disclosure of every other one on the line.
            if miss is not None:
                key = (miss.system_id if miss.system_id is not None
                       else ("phrase", str(miss.phrase or "").lower()))
                if key not in refused:
                    refused.add(key)
                    ignored.append(miss)
            i += 1
            continue
        phrase, sid, size, canon = hit
        i += size
        if sid in seen:
            continue
        seen.add(sid)
        # Casing polish only: take the bundled table's spelling when it is the
        # SAME name, so "svm-3k" renders "SVM-3K". Never substitute a different
        # name than the one matched — an injected resolver's id is its own
        # business, and quietly relabelling a row is how a range answer ends up
        # attributed to the wrong system.
        name = canon if (canon and canon.lower() == phrase.lower()) else phrase
        out.append(SystemRef(name, sid))
    # A system the message DID link is not an ignored one, however it was
    # spelled elsewhere on the line — the provenance line already names it, and
    # the two lines contradicting each other is worse than either being terse.
    return SystemMentions(out, [r for r in ignored if r.system_id not in seen])


# ── sources (the staging list) ───────────────────────────────────────────────

GROUP_HOSTILE = "HOSTILE"
GROUP_FRIENDLY = "FRIENDLY"
GROUP_LINKED = "LINKED"
#: Render order. Hostiles first: "who can reach me" is a threat question.
GROUP_ORDER = (GROUP_HOSTILE, GROUP_FRIENDLY, GROUP_LINKED)


@dataclass(frozen=True)
class SourceLists:
    """The configured staging picture: the primary friendly staging plus both
    configured lists. All three are DEFAULT SOURCES (rows of their own when the
    message linked nothing) and all three are what GROUPS a linked system —
    those two jobs are the same three fields on purpose, so a staging the report
    can classify is always a staging the report can also ask about.

    ``problems`` carries plain-ASCII complaints about config the app could not
    read, so a list it had to discard becomes a visible warning on the report
    instead of an empty table (see ``_names_in``)."""
    primary: SystemRef | None = None
    friendly: tuple = ()
    hostile: tuple = ()
    problems: tuple = ()

    @property
    def configured(self) -> bool:
        return bool(self.primary or self.hostile or self.friendly)

    def default_sources(self) -> list:
        """``[(ref, group)]`` — EVERY configured staging: primary + both lists.

        This used to emit the primary and the hostile list only. ``friendly``
        was consumed by ``group_of`` alone, so a system in it could be
        CLASSIFIED but never SOURCED: the owner's three configured friendly
        stagings produced no rows, and the one note that could have explained
        the gap (``NOTE_NO_FRIENDLY``) was keyed on the primary, so a set
        staging system suppressed even that. Reported live, 2026-07-29. Two
        lists edited side by side on one Settings page, one of which quietly
        does not answer the question the window exists to answer, is the
        "a missing row reads as nothing there" failure this module is built to
        prevent — read in the direction the module had not yet checked.

        Grouped THROUGH ``group_of``, so the two groupings cannot disagree.
        Constructing the groups by hand (primary -> FRIENDLY, hostile list ->
        HOSTILE) reads the same for every sane config and then diverges on the
        one config where the answer matters: a staging system ALSO listed as
        hostile rendered FRIENDLY here while ``group_of`` — the rule the LINKED
        path uses — called it HOSTILE. The same contradicted config answering
        "friend" or "foe" depending on whether the FC happened to name the
        system in the message is the worst shape that disagreement could take.
        HOSTILE is the safe direction, and it is the only one; that now covers
        a friendly-list entry contradicted by the hostile list too.

        ``group_of`` still special-cases the primary as FRIENDLY, so an owner
        whose staging system is not also listed under the friendly stagings does
        NOT see their own staging rendered as an unaligned LINKED row — that was
        the original reason for constructing the groups here, and it survives.

        Duplicates are emitted freely (the primary is commonly ALSO in the
        friendly list, and a contradicted config lists one system twice). They
        are collapsed by ``build_report``, which already de-duplicated this list
        with ``_same_system`` — deliberately NOT a second dedup here, because
        two rules for "same system" is how the two of them start disagreeing.
        Order therefore decides only which of two identical systems SURVIVES,
        and the primary leads so the FC's own staging keeps its own ref. The
        rendered ROW order is not this list's: ``build_report`` sorts by
        ``GROUP_ORDER`` (hostiles first), then nearest-first, degraded last."""
        out: list = []
        if self.primary is not None:
            out.append((self.primary, self.group_of(self.primary)))
        out.extend((r, self.group_of(r)) for r in self.friendly)
        out.extend((r, self.group_of(r)) for r in self.hostile)
        return out

    def group_of(self, ref) -> str:
        """FRIENDLY / HOSTILE by membership in the configured lists; LINKED when
        the system is in neither. Allegiance is never guessed. A system present
        in BOTH lists reads as HOSTILE — the safe direction for a contradicted
        config."""
        if any(_same_system(ref, r) for r in self.hostile):
            return GROUP_HOSTILE
        if _same_system(ref, self.primary) or any(
                _same_system(ref, r) for r in self.friendly):
            return GROUP_FRIENDLY
        return GROUP_LINKED


def _ref_of_name(name, resolve) -> SystemRef | None:
    text = str(name or "").strip()
    if not text:
        return None
    return SystemRef(text, _resolve_id(text, resolve))


#: Human labels for the two configured lists, used in ``NOTE_BAD_LIST``.
_LIST_LABELS = {
    "hostile_staging_systems": "Hostile staging list",
    "friendly_staging_systems": "Friendly staging list",
}
#: Warning for a configured list the app could not read. Plain ASCII — every
#: note in this module can reach ``log.*``, and this box's console is cp1252.
NOTE_BAD_LIST = "{} is unreadable - check Settings."


def _names_in(raw):
    """Names out of a configured staging list, or **None** when the value is
    present but unusable.

    Accepts any non-text, non-mapping iterable — list, tuple, set, generator —
    because the shape of the container was never the point; the names are.
    A bare STRING or a DICT is the dangerous case and is refused rather than
    coerced: iterating a string yields characters and a dict yields keys, so
    "helpfully" accepting either invents a staging list nobody configured. The
    refusal is loud (the caller turns None into a visible warning) because the
    old behaviour — quietly returning no names — dropped every hostile row and
    left an empty note, which is precisely the "a missing row reads as nothing
    there" failure this module exists to prevent."""
    if raw is None:
        return []
    if isinstance(raw, (str, bytes, bytearray, dict)):
        return None
    try:
        return list(raw)
    except TypeError:
        return None


def resolve_sources(config, resolve=None) -> SourceLists:
    """Build the ``SourceLists`` from the app config. Pure but for the resolver.

    * primary friendly = ``zkillboard.staging_system`` — THE staging system,
      system-wide, mirroring ``implant_reminder.resolve_staging``'s rung 2. No
      ``market.*`` key is read (drift-guarded), and neither is the implant
      reminder's own staging override: that override is that feature's, and
      inheriting it here would silently point the range check at a different
      system than Settings shows.
    * friendly / hostile lists = ``jump_range.friendly_staging_systems`` /
      ``hostile_staging_systems`` — the same lists the Jump Range tab edits.

    A name the resolver cannot place still comes back as an UNRESOLVED ref, so
    it can be rendered as a visible unresolved row rather than vanishing. A
    LIST the app cannot read at all becomes a ``problems`` entry for the same
    reason — silence is the one answer this module may not give."""
    cfg = config if isinstance(config, dict) else {}
    zk = cfg.get("zkillboard")
    zk = zk if isinstance(zk, dict) else {}
    jr = cfg.get("jump_range")
    jr = jr if isinstance(jr, dict) else {}
    resolver = system_coords.resolve_name if resolve is None else resolve
    problems: list[str] = []

    def _list(key):
        names = _names_in(jr.get(key))
        if names is None:
            problems.append(NOTE_BAD_LIST.format(_LIST_LABELS[key]))
            return ()
        refs = [_ref_of_name(n, resolver) for n in names]
        return tuple(r for r in refs if r is not None)

    return SourceLists(
        primary=_ref_of_name(zk.get("staging_system"), resolver),
        # Hostiles first so a config carrying BOTH problems reads threat-first,
        # the same order everything else in this module uses.
        hostile=_list("hostile_staging_systems"),
        friendly=_list("friendly_staging_systems"),
        problems=tuple(problems),
    )


# ── range maths ──────────────────────────────────────────────────────────────

#: The four hulls the summary answers for, each a different reach at JDC 5 —
#: which is the entire point of showing a column per hull. ``Capital`` is the
#: dreadnought range (7.0 LY), the hull that reaches where a titan (6.0) falls
#: short; ``Command Carrier`` (7.5 LY — 3.75 base x 2.0) reaches past both
#: capital columns and stops short of the Black Ops; ``Blops`` is the Black
#: Ops battleship (8.0 LY — 4.0 base x 2.0), which out-reaches all three, and
#: what it brings when it arrives is a covert bridge for the rest of its gang
#: — a threat question none of the other columns answer.
HULL_TITAN = "Titan"
HULL_CAPITAL = "Dreadnought"
#: The command carrier hull — its own ``SHIP_RANGES`` key, distinct from both
#: "Carrier" and "Force Auxiliary" (each 7.0 LY, same as the Dreadnought) —
#: at JDC-5 range 7.5 LY (3.75 base x 2.0), between the dreadnought and the
#: Black Ops. Owner-approved label below ("CC") abbreviates it to buy back
#: the width a fourth column costs — see the ceiling note beside
#: ``HULL_COLUMNS``.
HULL_CMD_CARRIER = "Command Carrier"
HULL_BLOPS = "Black Ops"
LABEL_TITAN = "Titan"
LABEL_CAPITAL = "Capital"
#: Owner-approved abbreviation (not "Cmd Carrier") — see ``HULL_CMD_CARRIER``.
LABEL_CMD_CARRIER = "CC"
#: The house's own short form — the Jump Range tab and the map's character
#: filter both already say "Blops", and the cell has to stay glanceable.
LABEL_BLOPS = "Blops"
#: ``(display label, SHIP_RANGES key)`` in render order. THE hull set: the
#: toast's columns, ``HULLS`` and therefore every ``RangeRow.reach`` mapping are
#: all derived from this one tuple, so a hull cannot be half-added.
#:
#: That covers the DATA layer only — the RENDER layer has a measured ceiling
#: this tuple cannot see. MEASURED 2026-09-19 on this box (96 dpi, tk scaling
#: 1.333 — the BASELINE every px constant here was measured at): 2 cols 616px,
#: 3 cols 690px, 4 cols 743px with the Command Carrier column abbreviated to
#: "CC" — 17px of slack under ``MAX_W`` (760). The retired "4 cols 760px"
#: figure was a CLAMPED reading, not real slack: ``_measure`` floors/ceils
#: against the ceiling, and a full-name 4th column ("Cmd Carrier") measures
#: 806px RAW, which reads back as 760 — the clamp hid the real content width.
#:
#: These pixel figures are DPI-specific: labels are sized in POINTS, so the
#: grid's width scales with tk scaling (= dpi/72) — 3-col/4-col-CC px go
#: 1.0: 522/565, 1.333: 690/743, 1.5: 696/751, 1.667: 783/844, 1.75: 864/929,
#: 2.0: 951/1022. Until 2026-09-19 the CEILING did not, so from ~125% Windows
#: display scaling upward the rightmost cells were clipped at the window edge
#: with no tell. The ceiling is now scaled by the same factor
#: (``client_toast.scaled_bounds``; the live value is ``RangeToast.max_size``),
#: and across a 0.01-step sweep of tk scaling 1.0..3.0 the slack under that
#: ceiling never drops below 24px ABOVE the baseline — tightest at 1.509,
#: where the Consolas 9pt advance steps 7px -> 8px.
#:
#: A FIFTH hull is a deliberate layout call even so: the 17px of baseline
#: slack is the binding constraint and it is the SMALLEST slack anywhere on
#: the sweep, so a column that does not fit at 96 dpi does not fit anywhere.
#: See ``test_the_fourth_column_still_fits_the_window_it_has_to_stay_readable``
#: (baseline) and ``test_the_row_grid_is_never_clipped_at_any_display_scaling``
#: (the sweep), and MEASURE before adding one.
HULL_COLUMNS = ((LABEL_TITAN, HULL_TITAN), (LABEL_CAPITAL, HULL_CAPITAL),
                (LABEL_CMD_CARRIER, HULL_CMD_CARRIER),
                (LABEL_BLOPS, HULL_BLOPS))
#: The ``SHIP_RANGES`` keys of ``HULL_COLUMNS``, in the same order.
HULLS = tuple(hull for _label, hull in HULL_COLUMNS)
#: PER-HULL fallback if a hull key ever leaves ``SHIP_RANGES`` — each hull's own
#: documented JDC-5 max. Never a silent 0 (which would render "out of range" for
#: everything) and deliberately never ONE number for every hull: the single 7.0
#: this replaced was correct for a dreadnought, generous for a titan and a
#: silent UNDER-report for a Black Ops, and under-reporting is the dangerous
#: direction for a feature answering "who can reach me".
_FALLBACK_LY = {HULL_TITAN: 6.0, HULL_CAPITAL: 7.0, HULL_CMD_CARRIER: 7.5,
                HULL_BLOPS: 8.0}
#: For a hull this module does not answer for at all: the widest range it knows,
#: so an unknown hull over-reports (safe) rather than under-reports. Derived, so
#: it cannot fall behind the table above.
_FALLBACK_UNKNOWN_LY = max(_FALLBACK_LY.values())


def hull_range_ly(hull) -> float:
    """JDC-5 max jump range for a hull, read LIVE off the CLASS dict
    ``jump_range.JumpRangeChecker.SHIP_RANGES``. Never a per-instance copy, and
    never a config value — see the module docstring for why that config key is
    deliberately dead here.

    A missing key falls back to that hull's own documented JDC-5 max and says so
    loudly; only a hull this module does not answer for at all lands on the
    shared last resort."""
    try:
        return float(jump_range.JumpRangeChecker.SHIP_RANGES[hull])
    except (KeyError, TypeError, ValueError):
        try:
            fallback = _FALLBACK_LY[hull]
        except (KeyError, TypeError):
            fallback = _FALLBACK_UNKNOWN_LY
        log.warning("range_check: no JDC-5 range for hull %r; using %.1f LY",
                    hull, fallback)
        return fallback


def reach_map(in_range_hulls=()) -> MappingProxyType:
    """A frozen ``hull -> in range`` mapping covering EXACTLY ``HULLS``.

    The ONE place a row's per-hull answers are built, so a row can never carry
    an answer for a hull the report does not render, nor miss one it does —
    which is what lets ``RangeRow.in_range`` be total. Frozen because a
    ``RangeRow`` is frozen, and a mutable field inside a frozen row is a frozen
    row in name only."""
    wanted = frozenset(in_range_hulls or ())
    return MappingProxyType({hull: hull in wanted for hull in HULLS})


#: The reach of every DEGRADED row — unresolved, no distance, same system. One
#: shared immutable object, so "we have no answer" is identical everywhere it is
#: meant, and never a hand-typed row of Falses that can drift a hull short.
NO_REACH = reach_map()


# ── the report ───────────────────────────────────────────────────────────────

ROW_OK = "ok"
ROW_UNRESOLVED = "unresolved"       # the NAME never resolved to a system
ROW_NO_DISTANCE = "no distance"     # resolved, but no distance was available
ROW_SAME_SYSTEM = "same system"     # source IS the target: cannot jump in-system

NOTE_TARGET_UNKNOWN = "Location unknown - no range answer."
NOTE_NO_SOURCES = "No staging systems configured."
#: The same-system row's note. Plain ASCII (log-safe, see the module docstring)
#: even though it only ever renders in the Tk toast today.
NOTE_SAME_SYSTEM = "same system - cannot jump within a system"
#: Non-blocking warning for the default-sources path with NO friendly source of
#: any kind: no ``zkillboard.staging_system`` (it defaults to "") AND an empty
#: ``jump_range.friendly_staging_systems``. Either one alone produces FRIENDLY
#: rows, so neither alone is a missing group. Both absent plus a configured
#: hostile list renders hostile rows and no FRIENDLY group at all, and absent
#: config explaining an absent group is the same silence as an absent row: the
#: FC reads "nobody friendly is in range" off a report that never asked the
#: question. (It used to key on the primary alone — back when the friendly list
#: was not a source, that WAS the whole condition; once the list contributes
#: rows, a primary-only test would fire over a report full of friendly rows.)
NOTE_NO_FRIENDLY = "No friendly staging configured - check Settings."
TITLE_FALLBACK = "Range check"


@dataclass(frozen=True)
class RangeRow:
    """One source system's answer. ``distance_ly`` is the RAW distance — the
    reach flags were computed from it unrounded; round only for display.

    ``reach`` is ONE frozen mapping keyed by hull (always built through
    ``reach_map`` / ``NO_REACH``), never a boolean field per hull. Parallel
    per-hull booleans is the shape that rots the moment a hull is added: every
    producer, every consumer and every test has to grow a field, and the one
    that does not keeps silently answering for the wrong hull.

    Latent limitation: ``frozen=True`` SYNTHESIZES ``__hash__``, so this class
    LOOKS hashable — but ``hash(row)`` now raises ``TypeError: unhashable
    type: 'dict'`` (and ``copy.deepcopy(row)`` raises ``TypeError: cannot
    pickle 'mappingproxy' object``) because of the ``reach`` mapping. Both
    worked when this row carried two plain bools instead. Nothing in this repo
    hashes, deep-copies, or ``dataclasses.asdict``s a row today, so this is
    latent, not broken — but a future ``set(report.rows)`` or ``asdict`` call
    WILL fail at runtime, not at review. If hashing a row is ever genuinely
    needed, switch ``reach`` to a tuple of ``(hull, bool)`` pairs rather than
    reaching for a custom ``__hash__``."""
    group: str
    name: str
    system_id: int | None
    distance_ly: float | None
    reach: MappingProxyType
    status: str = ROW_OK

    @property
    def ok(self) -> bool:
        return self.status == ROW_OK

    def in_range(self, hull) -> bool:
        """Total: a hull this row carries no answer for RAISES rather than
        answering. A silent ``False`` would render "out of range" — a perfectly
        plausible answer to a question the row cannot answer, and a plausible
        wrong answer is the one output this module may not produce."""
        try:
            return bool(self.reach[hull])
        except (KeyError, TypeError):
            raise KeyError(
                f"range_check: no reach answer for hull {hull!r}") from None


@dataclass(frozen=True)
class RangeReport:
    """What the toast renders.

    ``note`` is non-empty exactly when the answer is BLOCKED (no rows at all);
    ``warnings`` are non-blocking complaints that render ALONGSIDE the rows.
    Both are always plain ASCII (log-safe: this box's console is cp1252 and a
    fancy glyph in a log line raises inside the stream handler).

    ``linked`` holds the systems the MESSAGE named, and is the single source of
    truth for ``linked_used`` — the two cannot drift, and the toast can always
    say WHICH systems replaced the configured sources. ``ignored`` is its
    mirror: the phrases the message named that the gate REFUSED, so the other
    direction of that same decision is visible too (see ``ignored_line``)."""
    target: SystemRef | None = None
    rows: tuple = ()
    linked: tuple = ()
    note: str = ""
    warnings: tuple = ()
    ignored: tuple = ()

    @property
    def target_known(self) -> bool:
        return self.target is not None and self.target.resolved

    @property
    def linked_used(self) -> bool:
        """Did the message override the configured sources? Derived, never
        stored: a stored flag beside the list is a flag that can disagree with
        it, and this one decides whether the FC is shown WHY the report looks
        the way it does."""
        return bool(self.linked)

    @property
    def title(self) -> str:
        if not self.target_known:
            return TITLE_FALLBACK
        if self.target.name:
            return f"Range to {self.target.name}"
        return f"Range to system {self.target.system_id}"

    def groups(self) -> list:
        """``[(group, [rows])]`` in render order, empty groups omitted."""
        out = []
        for group in GROUP_ORDER:
            members = [r for r in self.rows if r.group == group]
            if members:
                out.append((group, members))
        return out


def _distance(distance_fn, a_id, b_id):
    """Injected distance, hardened: any failure means "no distance", which is a
    visible row, not a dropped one."""
    if distance_fn is None:
        return None
    try:
        raw = distance_fn(a_id, b_id)
    except Exception:
        log.debug("range_check: distance %s->%s failed", a_id, b_id, exc_info=True)
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if value != value or value < 0:        # NaN / nonsense
        return None
    return value


def _ignored_refs(raw, linked_refs) -> tuple:
    """Normalize refused phrases, dropping any system the message actually used.

    Accepted and ignored are mutually exclusive HERE as well as in
    ``extract_systems``, so a caller assembling a report by hand cannot produce
    one that claims to have both used and dropped the same system. De-duplicated
    by system id; a phrase-less entry is not a disclosure and is dropped."""
    used = {r.system_id for r in linked_refs if r.resolved}
    out: list[IgnoredRef] = []
    seen: set = set()
    for item in (raw or ()):
        phrase = str(getattr(item, "phrase", "") or "").strip()
        if not phrase:
            continue
        sid = getattr(item, "system_id", None)
        sid = sid if _is_id(sid) else None
        if sid is not None and (sid in used or sid in seen):
            continue
        if sid is not None:
            seen.add(sid)
        out.append(item if isinstance(item, IgnoredRef) else IgnoredRef(
            phrase, sid, str(getattr(item, "retype", "") or ""),
            str(getattr(item, "reason", "") or "")))
    return tuple(out)


def build_report(target, linked=(), *, ignored=None, sources=None,
                 distance_fn=None, resolve=None) -> RangeReport:
    """Compute the range summary.

    ``target`` is the posting character's current system (a ``SystemRef``, a
    bare system id, a NAME, or None when the location is unknown). ``linked`` is
    whatever ``extract_systems`` found in the message: **a non-empty ``linked``
    REPLACES the configured sources entirely** — the FC named the systems they
    want the answer for, and ``provenance_line`` says so on the face of the
    report so that replacement is never a silent one. ``sources`` is a
    ``SourceLists`` (from ``resolve_sources``); ``distance_fn(a_id, b_id) -> ly``
    is injected (``jump_range.calculate_ly_distance`` in the app); ``resolve``
    names bare-string targets (default ``system_coords.resolve_name``).

    ``ignored`` is the REFUSED half of the same extraction. It defaults to
    reading ``linked.ignored`` — ``extract_systems`` returns a
    ``SystemMentions`` carrying it — so a wiring that already passes the
    accepted systems discloses the refused ones for free, and the two halves
    can never be computed from different chat lines. Pass it to override.

    Honesty rules, all tested: an unknown target produces a report that SAYS
    the location is unknown rather than a plausible wrong answer; a source
    name that never resolved becomes an unresolved row; a resolved source with
    no available distance becomes a no-distance row; a source that IS the
    target's own system becomes a ``ROW_SAME_SYSTEM`` caution row rather than a
    green 0.00 ly in-range answer (a capital cannot jump within its own
    system). Rows are ordered hostiles-then-friendlies-then-linked, nearest
    first inside each group, with degraded (and same-system) rows last so they
    read as exceptions rather than as zero-distance. Pure."""
    srcs = sources if isinstance(sources, SourceLists) else SourceLists()
    notes = list(srcs.problems)
    target_ref = _as_ref(target, resolve)

    linked_refs = []
    for item in (linked or ()):
        ref = _as_ref(item, resolve)
        if ref is None or not (ref.name or ref.resolved):
            continue
        if not any(_same_system(ref, seen) for seen in linked_refs):
            linked_refs.append(ref)
    linked_tuple = tuple(linked_refs)
    ignored_tuple = _ignored_refs(
        getattr(linked, "ignored", ()) if ignored is None else ignored,
        linked_refs)

    if linked_refs:
        picked = [(ref, srcs.group_of(ref)) for ref in linked_refs]
    else:
        picked = []
        for ref, group in srcs.default_sources():
            if any(_same_system(ref, prev) for prev, _g in picked):
                continue
            picked.append((ref, group))
        # No friendly SOURCE at all — neither Settings > Staging System nor a
        # single friendly staging list entry — renders hostile rows under no
        # FRIENDLY header, indistinguishable from "no friendly staging is in
        # range". Both halves are tested for PRESENCE, not resolvability: an
        # unresolvable name is not this case, because it becomes a visible
        # unresolved FRIENDLY row that explains itself already. An UNREADABLE
        # friendly list reaches here as an empty tuple and so does warn — and
        # should: it carries its own NOTE_BAD_LIST saying why, and the pair
        # reads as cause plus consequence rather than as one thing said twice.
        if srcs.primary is None and not srcs.friendly and srcs.hostile:
            notes.append(NOTE_NO_FRIENDLY)
    warnings = tuple(notes)

    # A degraded report still carries ``linked``, ``ignored`` and ``warnings``:
    # those are the lines that explain WHY it is degraded, and dropping them
    # here is how "your message replaced the sources" — or "your message named
    # a system I refused" — becomes "nothing can reach us".
    if target_ref is None or not target_ref.resolved:
        return RangeReport(target=target_ref, rows=(), linked=linked_tuple,
                           note=NOTE_TARGET_UNKNOWN, warnings=warnings,
                           ignored=ignored_tuple)
    if not picked:
        return RangeReport(target=target_ref, rows=(), linked=linked_tuple,
                           note=NOTE_NO_SOURCES, warnings=warnings,
                           ignored=ignored_tuple)

    # One live read per hull per report, and only once the report is going to
    # HAVE rows: a blocked report must not log a fallback warning for an answer
    # it never gives.
    ranges = {hull: hull_range_ly(hull) for hull in HULLS}

    rows: list[RangeRow] = []
    for ref, group in picked:
        if not ref.resolved:
            rows.append(RangeRow(group, ref.name, None, None, NO_REACH,
                                 ROW_UNRESOLVED))
            continue
        if ref.system_id == target_ref.system_id:
            # Checked by id BEFORE distance_fn is ever called: a capital
            # cannot jump within its own system, and that fact is knowable
            # from identity alone. Deciding it this way means a distance_fn
            # that happens to answer 0.0 for a same-system pair can never
            # silently reintroduce the green zero-distance row this exists
            # to prevent.
            rows.append(RangeRow(group, ref.name, ref.system_id, 0.0,
                                 NO_REACH, ROW_SAME_SYSTEM))
            continue
        dist = _distance(distance_fn, ref.system_id, target_ref.system_id)
        if dist is None:
            rows.append(RangeRow(group, ref.name, ref.system_id, None,
                                 NO_REACH, ROW_NO_DISTANCE))
            continue
        # RAW distance against the raw range, every hull decided the same way.
        rows.append(RangeRow(
            group, ref.name, ref.system_id, dist,
            reach_map(h for h in HULLS if dist <= ranges[h]), ROW_OK))

    order = {g: i for i, g in enumerate(GROUP_ORDER)}
    rows.sort(key=lambda r: (order.get(r.group, len(order)),
                             0 if r.ok else 1,
                             r.distance_ly if r.distance_ly is not None else 0.0))
    return RangeReport(target=target_ref, rows=tuple(rows),
                       linked=linked_tuple, note="", warnings=warnings,
                       ignored=ignored_tuple)


# ── render helpers (pure) ────────────────────────────────────────────────────

#: GUI text only — these are not cp1252 and MUST never reach ``log.*``.
MARK_IN = "✓"       # check mark
MARK_OUT = "✕"      # multiplication x
MARK_CAUTION = "⚠"  # same-system: cannot jump, not an out/in-range verdict


def hull_cell(label, in_range) -> str:
    """``"Titan ✓"`` / ``"Titan ✕"``. The glyph carries the answer as well as
    the colour, so the toast is readable without relying on red/green alone."""
    return f"{label} {MARK_IN if in_range else MARK_OUT}"


def same_system_cell(label) -> str:
    """``"Titan ⚠"`` — the ``ROW_SAME_SYSTEM`` cell. Deliberately neither
    ``MARK_IN`` nor ``MARK_OUT``: a capital cannot jump within its own system,
    which is neither an in-range nor an out-of-range verdict, and red/green
    stay reserved for those two answers alone."""
    return f"{label} {MARK_CAUTION}"


def format_distance(distance_ly) -> str:
    """Display form of a RAW distance. Rounding happens here and nowhere else —
    the in-range flags were decided on the unrounded value."""
    if distance_ly is None:
        return ""
    return f"{distance_ly:.2f} ly"


def row_note(row) -> str:
    """The dim explanation for a degraded row; "" for a normal one."""
    if row.status == ROW_UNRESOLVED:
        return "unknown system"
    if row.status == ROW_NO_DISTANCE:
        return "no distance"
    if row.status == ROW_SAME_SYSTEM:
        return NOTE_SAME_SYSTEM
    return ""


#: How much of ONE name or phrase either disclosure line spells out before it
#: clips. Every real system name is well inside it (the longest K-space name is
#: 14 characters); a 500-character token an FC pasted after the keyword is not,
#: and one of those makes the toast measure past ``MAX_W`` -- where it is not
#: the long word that is lost but the whole line, clipped at the window edge
#: with no ellipsis to say so. Clipping HERE keeps the rest of the line
#: readable, which is the half that carries the meaning.
MAX_DISCLOSED_CHARS = 32


def _clip(text) -> str:
    """``text`` cut to ``MAX_DISCLOSED_CHARS`` with an ASCII ellipsis (the
    disclosure lines are log-safe and this box's console is cp1252)."""
    out = str(text or "")
    if len(out) <= MAX_DISCLOSED_CHARS:
        return out
    return out[:MAX_DISCLOSED_CHARS - 3] + "..."


#: Lead-in for the provenance line. Plain ASCII: it can reach ``log.*``.
PROVENANCE_PREFIX = "sources from message: "
#: How many linked names are spelled out before the line summarises the rest.
MAX_PROVENANCE_NAMES = 6


def provenance_line(report) -> str:
    """Where this report's sources came from — "" unless the MESSAGE set them.

    LOAD-BEARING honesty, not decoration. A linked system replaces the
    configured stagings outright, so one word the parser took for a system name
    turns "who can reach me" into a one-row report with no HOSTILE group in it
    at all. Without this line an FC cannot tell that apart from "nothing can
    reach us" — the difference between "that misparsed, retype it" and "we are
    safe, dock up". It renders on a DEGRADED report too, where it is the only
    surviving evidence of what happened.

    A linked ref with no name (possible: an id-only ref) still gets counted, so
    the line is never empty while the override is in force."""
    names = []
    for ref in getattr(report, "linked", ()) or ():
        text = str(getattr(ref, "name", "") or "").strip()
        if not text:
            sid = getattr(ref, "system_id", None)
            text = f"system {sid}" if sid else ""
        if text:
            names.append(_clip(text))
    if not names:
        return ""
    shown = names[:MAX_PROVENANCE_NAMES]
    extra = len(names) - len(shown)
    text = PROVENANCE_PREFIX + ", ".join(shown)
    return f"{text} (+{extra} more)" if extra else text


#: Lead-in for the ignored line. Plain ASCII: it can reach ``log.*``.
IGNORED_PREFIX = "ignored in message: "
#: How the line advises a fix, when one exists (see ``_retype_hint``).
RETYPE_HINT = "{} (type it as {})"
#: How the line explains an entry no retyping can fix (see ``REASON_UNKNOWN``).
REASON_HINT = "{} ({})"
#: How many refused phrases are spelled out. Deliberately below the provenance
#: cap: each entry can carry a "(type it as X)" tail, so three is already a
#: long line on a window that must stay glanceable.
MAX_IGNORED_NAMES = 3


def ignored_line(report) -> str:
    """What the message named and the gate REFUSED — "" when it refused nothing.

    The mirror of ``provenance_line``, and load-bearing for the same reason
    read the other way round. The plain-phrase gate refuses SILENTLY, so
    ``range check jita`` produced the configured-sources report — byte-for-byte
    the shape of "you named no systems" — while ``range check Jita`` produced a
    single LINKED row. Measured on the owner's 304k-message Fleet history the
    gate refuses 27.3% of mid-sentence real-name mentions and 58.6% across all
    positions, overwhelmingly lowercase typing. An FC who typed a system name
    and got the default report had no way at all to tell "I refused that word"
    from "you did not name anything" — and "silence is the one answer this
    module may not give" has to hold in the refusal direction too.

    Deliberately advisory, not corrective: it does not weaken the gate, and it
    renders dim and secondary. It also names refusals no retyping can fix (a
    three-letter name, a sentence-initial one) WITHOUT advice, because naming
    what was dropped is the point and a fix that does not work is worse than
    none. The cost is the honest one — ``"range check on my toon"`` now says it
    ignored ``toon``, which is exactly what it did."""
    items = []
    for entry in getattr(report, "ignored", ()) or ():
        phrase = str(getattr(entry, "phrase", "") or "").strip()
        if not phrase:
            continue
        phrase = _clip(phrase)
        retype = str(getattr(entry, "retype", "") or "").strip()
        if retype:
            items.append(RETYPE_HINT.format(phrase, _clip(retype)))
            continue
        # No retyping fixes a token that named nothing (or named several), so
        # the entry says WHY instead of offering advice that cannot work.
        reason = str(getattr(entry, "reason", "") or "").strip()
        items.append(REASON_HINT.format(phrase, _clip(reason))
                     if reason else phrase)
    if not items:
        return ""
    shown = items[:MAX_IGNORED_NAMES]
    extra = len(items) - len(shown)
    text = IGNORED_PREFIX + ", ".join(shown)
    return f"{text} (+{extra} more)" if extra else text


def row_budget(group_sizes, cap) -> list:
    """How many rows each group may render under ``cap``, in render order.

    Every non-empty group gets its FIRST row — and therefore its header —
    before any group takes surplus. Filling greedily in group order, which is
    what this used to do, meant twelve hostile stagings pushed the entire
    FRIENDLY group into "+N more": the owner's own staging row, the one that
    answers "can WE still get there", was the first thing to disappear from a
    window whose whole subject is reach. Surplus still goes hostiles-first,
    because "who can reach me" is the threat question.

    Pure and Tk-free so the starvation is testable without a display."""
    try:
        room = max(0, int(cap))
    except (TypeError, ValueError):
        room = MAX_ROWS
    sizes = []
    for size in group_sizes or ():
        try:
            sizes.append(max(0, int(size)))
        except (TypeError, ValueError):
            sizes.append(0)
    take = [0] * len(sizes)
    for i, size in enumerate(sizes):          # guarantee pass
        if room <= 0:
            break
        if size > 0:
            take[i] = 1
            room -= 1
    for i, size in enumerate(sizes):          # surplus pass, in render order
        if room <= 0:
            break
        extra = min(room, size - take[i])
        if extra > 0:
            take[i] += extra
            room -= extra
    return take


# ── the toast ────────────────────────────────────────────────────────────────

#: Group header colours. Deliberately NOT red/green: in this window red and
#: green mean "out of range" / "in range" and nothing else.
GROUP_COLORS = {
    GROUP_HOSTILE: FG_ORANGE,
    GROUP_FRIENDLY: FG_ACCENT,
    GROUP_LINKED: FG_YELLOW,
}
#: Row cap, so a long staging list cannot grow a screen-tall window.
MAX_ROWS = 12
#: Config warnings rendered before the rest are folded away. Two unreadable
#: staging lists plus the missing-primary note is the whole surface, so three
#: shows all of it — raise this in step with any new warning.
MAX_WARNINGS = 3
#: Row grid: the name in column 0, one column per hull, then the distance/note.
#: DERIVED from ``HULL_COLUMNS`` rather than typed, so adding a hull cannot
#: leave the distance column sitting on top of the last hull cell (or a header's
#: ``columnspan`` stopping short of it).
_COL_DISTANCE = len(HULL_COLUMNS) + 1
_GRID_SPAN = _COL_DISTANCE + 1
#: Size floor/ceiling in px at the BASELINE display scaling (96 dpi / tk
#: scaling 1.333 — see ``RangeToast._measure`` on the unit). The ceilings are
#: scaled UP at runtime by ``client_toast.scaled_bounds``, because every label
#: here is sized in POINTS and so grows with the monitor's dpi while a fixed px
#: ceiling would not: the effective ceiling is ``RangeToast.max_size``, and
#: that — not ``MAX_W``/``MAX_H`` — is what a measurement should be compared
#: against anywhere but a 96-dpi box. At the baseline the 4th (Command
#: Carrier, "CC") column measures 743px, 17px of slack under 760; see the
#: measurement + scaling-sweep note beside ``HULL_COLUMNS`` before adding a
#: 5th. The FLOORS are deliberately NOT scaled: they exist to stop a two-line
#: toast looking like a sliver, which is a px judgement, not a text one.
MIN_W, MAX_W = 260, 760
MIN_H, MAX_H = 60, 560
_FONT = "Consolas"


class RangeToast:
    """The range summary as a transient over-client window. Tk-thread only.

    A deliberate sibling of ``client_toast.ClientToast``, not a subclass and not
    a reuse of it: that class renders one title + one body string, while this
    needs a per-CELL red/green grid. ``ClientToast`` is shipped and verified;
    bending its API to fit this is how it breaks. What IS shared is the pure
    geometry helper ``place_over`` and the fade/re-top constants, so the two
    toasts cannot drift apart in placement or feel.

    House rules honoured (see ``client_toast``'s docstring for the full account):
    placement only via ``win32.set_window_pos`` in physical px — never Tk
    ``geometry()``; ex-styles re-asserted after EVERY alpha change because Tk's
    ``-alpha`` rewrites the whole ex-style word; it positions its OWN hwnd only.

    Lifecycle: construct (builds hidden and measures), ``show(rect)`` places and
    maps, the hold timer fades and destroys. ``dismiss()`` is idempotent. A left
    click dismisses early."""

    def __init__(self, root, report, *, win32=None, seconds=TOAST_SECONDS,
                 on_dismiss=None, accent=FG_ACCENT, max_rows=MAX_ROWS):
        if win32 is None:                                  # pragma: no cover
            from preview_tile import _real_tile_win32
            win32 = _real_tile_win32()
        self._win32 = win32
        self._on_dismiss = on_dismiss
        self.report = report
        try:
            self._seconds = max(0.5, float(seconds))
        except (TypeError, ValueError):
            self._seconds = TOAST_SECONDS
        self._alive = True
        self._after_ids: list = []
        self._alpha = ALPHA
        self._clickable: list = []

        self.top = tk.Toplevel(root)
        self.top.overrideredirect(True)
        self.top.configure(bg=accent)          # 1px accent border via padding
        self.top.withdraw()

        inner = tk.Frame(self.top, bg=BG_PANEL)
        inner.pack(fill="both", expand=True, padx=1, pady=1)
        self._clickable.extend([self.top, inner])

        self._build(inner, accent, max_rows)

        # Measure while STILL WITHDRAWN — an idle flush here cannot flash the
        # window, and show() must not flush at all (the one-frame-flash trap).
        self._w, self._h = self._measure()

        self._hwnd = self._win32.get_root_hwnd(self.top.winfo_id())
        self._restyle()
        for w in self._clickable:
            w.bind("<Button-1>", self._on_click)

    # ── build ────────────────────────────────────────────────────────────────
    def _label(self, parent, text, fg, size=9, bold=False, **kw):
        font = (_FONT, size, "bold") if bold else (_FONT, size)
        lbl = tk.Label(parent, text=text, bg=BG_PANEL, fg=fg, anchor="w",
                       justify="left", font=font, **kw)
        self._clickable.append(lbl)
        return lbl

    def _build(self, inner, accent, max_rows):
        report = self.report
        self._label(inner, report.title, accent, size=11, bold=True).pack(
            fill="x", padx=8, pady=(6, 2))
        rule = tk.Frame(inner, bg=BORDER_COLOR, height=1)
        rule.pack(fill="x", padx=8)
        self._clickable.append(rule)

        # Directly under the title, because it explains the table below it: a
        # report whose sources came from the message must SAY so, degraded or
        # not (see ``provenance_line``).
        provenance = provenance_line(report)
        if provenance:
            self._label(inner, provenance, FG_DIM, size=7).pack(
                fill="x", padx=8, pady=(3, 0))
        # Its mirror, same dim treatment and the same header slot, so it
        # survives a degraded report: what the message named and the gate
        # refused. Never red/green — in this window those two colours mean
        # out-of-range / in-range and nothing else, and this is an advisory.
        ignored = ignored_line(report)
        if ignored:
            self._label(inner, ignored, FG_DIM, size=7).pack(
                fill="x", padx=8, pady=(3, 0))

        body = tk.Frame(inner, bg=BG_PANEL)
        body.pack(fill="both", expand=True, padx=8, pady=(4, 0))
        self._clickable.append(body)

        line = 0
        if report.note:
            self._label(body, report.note, FG_YELLOW).grid(
                row=line, column=0, columnspan=_GRID_SPAN, sticky="w")
            line += 1
        else:
            line = self._fill_rows(body, max_rows)
        # Warnings render ALONGSIDE the rows, never instead of them: an
        # unreadable staging list must not be able to look like a clean answer.
        for warning in (report.warnings or ())[:MAX_WARNINGS]:
            self._label(body, warning, FG_YELLOW).grid(
                row=line, column=0, columnspan=_GRID_SPAN, sticky="w",
                pady=(2, 0))
            line += 1

        self._label(inner, "click to dismiss", FG_DIM, size=7).pack(
            fill="x", padx=8, pady=(3, 4))

    def _fill_rows(self, body, max_rows):
        """Grid the grouped rows; returns the next free grid row so ``_build``
        can put the config warnings UNDER them rather than over them."""
        try:
            cap = max(1, int(max_rows))
        except (TypeError, ValueError):
            cap = MAX_ROWS
        groups = self.report.groups()
        budget = row_budget([len(rows) for _g, rows in groups], cap)
        line = 0
        hidden = 0
        for (group, rows), take in zip(groups, budget):
            if take <= 0:
                hidden += len(rows)
                continue
            self._label(body, group, GROUP_COLORS.get(group, FG_DIM),
                        bold=True).grid(row=line, column=0,
                                        columnspan=_GRID_SPAN,
                                        sticky="w", pady=(2, 0))
            line += 1
            for row in rows[:take]:
                self._grid_row(body, line, row)
                line += 1
            hidden += max(0, len(rows) - take)
        if hidden:
            self._label(body, f"+{hidden} more", FG_DIM, size=7).grid(
                row=line, column=0, columnspan=_GRID_SPAN, sticky="w")
            line += 1
        return line

    def _grid_row(self, body, line, row):
        self._label(body, row.name, FG_TEXT).grid(
            row=line, column=0, sticky="w", padx=(6, 10))
        if row.status == ROW_SAME_SYSTEM:
            # Caution, not a verdict: no hull jumps within its own system, so
            # EVERY hull cell carries the same yellow warning instead of a
            # red/green answer that would say the opposite. Driven off
            # HULL_COLUMNS, so a new column cannot quietly render a tick here.
            for col, (label, _hull) in enumerate(HULL_COLUMNS, start=1):
                self._label(body, same_system_cell(label), FG_YELLOW).grid(
                    row=line, column=col, sticky="w", padx=(0, 10))
            self._label(body, row_note(row), FG_DIM).grid(
                row=line, column=_COL_DISTANCE, sticky="w")
        elif row.ok:
            for col, (label, hull) in enumerate(HULL_COLUMNS, start=1):
                good = row.in_range(hull)
                self._label(body, hull_cell(label, good),
                            FG_GREEN if good else FG_RED).grid(
                    row=line, column=col, sticky="w", padx=(0, 10))
            self._label(body, format_distance(row.distance_ly), FG_DIM).grid(
                row=line, column=_COL_DISTANCE, sticky="w")
        else:
            # Never render a flag we do not have: a degraded row says why, so a
            # missing answer can't read as "out of range" (or as "in range").
            self._label(body, row_note(row), FG_YELLOW).grid(
                row=line, column=1, columnspan=_GRID_SPAN - 1, sticky="w")

    def _measure(self):
        """Content size in px, floored and capped at the DPI-SCALED ceiling.

        Tk lays out in logical px while ``set_window_pos`` takes physical px;
        the shipped ``ClientToast`` has the same one-unit assumption (its fixed
        430x78 box is treated as physical while its labels are laid out by Tk).
        Measuring keeps the two in the same relationship rather than hard-coding
        a box the content may not fit in.

        ``MAX_W``/``MAX_H`` are the 96-dpi figures, so the ceiling is resolved
        through ``scaled_bounds`` FIRST: the labels are point-sized and grow
        with the monitor, and a fixed px ceiling turns that growth into a
        silently clipped hull verdict from ~125% Windows scaling upward. The
        result is cached on the instance and published as ``max_size``, so a
        caller (and a test) can tell "fits" from "clamped"."""
        self._max_w, self._max_h = scaled_bounds(self.top, MAX_W, MAX_H)
        try:
            self.top.update_idletasks()
            w = int(self.top.winfo_reqwidth())
            h = int(self.top.winfo_reqheight())
        except tk.TclError:                                # pragma: no cover
            return MIN_W, MIN_H
        return (max(MIN_W, min(w + 2, self._max_w)),
                max(MIN_H, min(h + 2, self._max_h)))

    # ── internals (mirrors ClientToast) ──────────────────────────────────────
    def _restyle(self):
        try:
            self._win32.exclude_from_alt_tab(self._hwnd)
        except Exception:
            pass

    def _set_alpha(self, a):
        self._alpha = a
        try:
            self.top.attributes("-alpha", a)
        except tk.TclError:
            return
        self._restyle()

    def _after(self, ms, fn):
        if not self._alive:
            return
        try:
            self._after_ids.append(self.top.after(int(ms), fn))
        except tk.TclError:
            pass

    def _cancel_timers(self):
        for aid in self._after_ids:
            try:
                self.top.after_cancel(aid)
            except (tk.TclError, ValueError):
                pass
        self._after_ids = []

    def _on_click(self, _ev=None):
        self.dismiss()

    def _retop_once(self):
        if not self._alive:
            return
        try:
            self._win32.retop(self._hwnd)
        except Exception:
            pass

    def _begin_fade(self, step=0):
        if not self._alive:
            return
        if step >= FADE_STEPS:
            self.dismiss()
            return
        self._set_alpha(ALPHA * (1.0 - (step + 1) / float(FADE_STEPS)))
        self._after(max(1, FADE_MS // FADE_STEPS),
                    lambda: self._begin_fade(step + 1))

    # ── public surface ───────────────────────────────────────────────────────
    @property
    def hwnd(self):
        return self._hwnd

    @property
    def alive(self) -> bool:
        return self._alive

    @property
    def size(self):
        return (self._w, self._h)

    @property
    def max_size(self):
        """The EFFECTIVE ``(width, height)`` ceiling this toast was measured
        against — ``(MAX_W, MAX_H)`` scaled for the display's tk scaling.

        ``size == max_size`` on an axis means the content was CLAMPED there
        (and is therefore clipped at the window edge); ``size < max_size``
        means it fits. Comparing a measurement against the bare ``MAX_W``
        constant only says anything on a 96-dpi box."""
        return (self._max_w, self._max_h)

    def current_alpha(self):
        return self._alpha

    def show(self, client_rect, fallback_xy=None):
        """Place over ``client_rect`` (EDGES, physical px) and start the hold.

        Nothing may flush the event loop between the map and the move — the
        Toplevel carries no Tk geometry, so a stray ``update_idletasks`` here
        flashes it opaque at Tk's default position for one frame before it jumps
        over the client (measuring happens in the constructor, while withdrawn,
        for exactly this reason)."""
        if not self._alive:
            return False
        xy = place_over(client_rect, self._w, self._h)
        if xy is None:
            xy = fallback_xy
        if xy is None:
            self.dismiss()
            return False
        x, y = int(xy[0]), int(xy[1])
        self._set_alpha(0.0)
        try:
            self.top.deiconify()
        except tk.TclError:
            return False
        try:
            self._win32.set_window_pos(self._hwnd, x, y, self._w, self._h)
        except Exception:
            pass
        self._set_alpha(ALPHA)
        self._after(RETOP_MS, self._retop_once)
        self._after(int(self._seconds * 1000), self._begin_fade)
        return True

    def dismiss(self):
        """Destroy the toast. Idempotent; safe from a bound callback."""
        if not self._alive:
            return
        self._alive = False
        self._cancel_timers()
        try:
            self.top.destroy()
        except tk.TclError:
            pass
        cb = self._on_dismiss
        if cb is not None:
            try:
                cb()
            except Exception:
                pass
