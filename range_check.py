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
  x 2.0, i.e. Titan 6.0, Dreadnought 7.0. This module deliberately does NOT
  route through ``JumpRangeChecker(custom_ranges=...)`` and reads NO per-hull
  range out of config: that config key currently drives nothing at all in the
  app, and a config persisted before v2.8.1 can still carry pre-fix values that
  would silently override a corrected default and produce a WRONG range answer.
  Honouring it here would promote a dead key back to live as a side effect of
  this feature. If per-hull customisation is ever wanted it needs a deliberate
  config migration. (Drift guard:
  ``test_range_check.py::test_the_module_reads_no_per_hull_range_config_key``.)
* **Linked systems REPLACE the source list, never the target.** A message naming
  systems is the FC asking "range from THESE", so the configured stagings step
  aside entirely.
* **A name that will not resolve becomes an unresolved ROW, never a dropped
  one.** A missing row reads as "nothing there", which is the dangerous failure
  direction for a feature whose whole job is telling you who can reach you. Same
  for a distance the injected function cannot supply, and same for an unknown
  target: the report says so instead of answering.
* **Allegiance is never guessed.** A linked system is FRIENDLY or HOSTILE only
  by membership in the configured lists; anything else lands in its own LINKED
  group. A system configured into BOTH lists resolves as HOSTILE — the safe
  error direction for a config the owner has contradicted.
* **The staging is Settings > Staging System** (``zkillboard.staging_system``),
  system-wide, mirroring ``implant_reminder.resolve_staging``'s rung 2. No
  ``market.*`` key is read here, and neither is the implant reminder's own
  staging override (that override belongs to that feature, not this one).
* Distances are compared RAW and only rounded for display — never compare a
  rounded float (the security-band lesson).

Threading: the chat tail runs on a worker thread. Everything in the engine is
safe there; ``RangeToast`` is Tk-thread only, so the caller marshals the show
through ``FCToolGUI._post_ui``.
"""
from __future__ import annotations

import time
import tkinter as tk
from dataclasses import dataclass
from typing import NamedTuple

import jump_range
import system_coords
from app_log import get_logger
from chat_monitor import MESSAGE_PATTERN
from client_toast import ALPHA, FADE_MS, FADE_STEPS, RETOP_MS, place_over
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
    "enabled": False,            # MASTER GATE — opt-in: it reacts to chat and
                                 # draws over a client, so it starts OFF.
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
    (False); never raises."""
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


def should_fire(sender, body, *, keyword, own_keys, last_fired=None,
                now=None, cooldown_s=COOLDOWN_SECONDS) -> bool:
    """Should this chat line pop a range check? Pure — nothing is mutated.

    All four gates must pass: a non-blank keyword present in the body, a sender
    that is one of the owner's OWN characters, and no fire from that same
    character within ``cooldown_s``. ``last_fired`` is a read-only mapping of
    sender key -> last fire timestamp (same clock as ``now``); the caller stamps
    it (or uses ``RangeCheckTrigger``, which owns the stamping)."""
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
    stamp = time.time() if now is None else float(now)
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
        """True (and the cooldown is stamped) iff this line should fire."""
        stamp = time.time() if now is None else float(now)
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


def _as_ref(value) -> SystemRef | None:
    """Accept a ``SystemRef``, a bare system id, or a ``(name, id)`` pair.

    The bare-id form names itself off the bundled offline table
    (``system_coords.get_name``) so a caller holding only the ESI poller's
    ``solar_system_id`` still gets "Range to J5A-IX" rather than a raw number.
    That is an in-memory read of shipped data — never a network call."""
    if value is None:
        return None
    if isinstance(value, SystemRef):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        if not _is_id(value):
            return None
        return SystemRef(system_coords.get_name(value) or "", value)
    if isinstance(value, str):
        return SystemRef(value.strip()) if value.strip() else None
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


def extract_systems(body, resolve=None) -> list[SystemRef]:
    """System names mentioned in a chat body, in order of first appearance.

    Same shape as the shipped intel detection (``intel_stream._system_spans``):
    slide 1->3-word windows across the line and take a window as a system iff
    the resolver names it, longest window first, single-word matches under 3
    characters ignored. The resolver is INJECTED (default
    ``system_coords.resolve_name``, the bundled offline table) so this stays
    pure and so a caller can supply an ESI-backed one; it is never a novel fuzzy
    matcher, because a fuzzy hit here would silently answer about the wrong
    system.

    A linked system renders in the log as its BARE NAME (verified against the
    owner's real Fleet logs, 2026-07-27) — there is no markup to strip. Results
    are de-duplicated by system id."""
    text = str(body or "")
    resolver = system_coords.resolve_name if resolve is None else resolve

    def _resolve(phrase):
        try:
            sid = resolver(phrase)
        except Exception:
            return None
        try:
            sid = int(sid)
        except (TypeError, ValueError):
            return None
        return sid if sid > 0 else None

    words = [(m.group(0), m.start(), m.end())
             for m in SYSTEM_TOKEN_RE.finditer(text)]
    out: list[SystemRef] = []
    seen: set[int] = set()
    i, n = 0, len(words)
    while i < n:
        hit = None
        for size in (3, 2, 1):
            if i + size > n:
                continue
            phrase = " ".join(w for w, _s, _e in words[i:i + size])
            if size == 1 and len(phrase) < 3:
                continue
            sid = _resolve(phrase)
            if sid is not None:
                hit = (phrase, sid, size)
                break
        if hit is None:
            i += 1
            continue
        phrase, sid, size = hit
        i += size
        if sid in seen:
            continue
        seen.add(sid)
        # Casing polish only: take the bundled table's spelling when it is the
        # SAME name, so "svm-3k" renders "SVM-3K". Never substitute a different
        # name than the one matched — an injected resolver's id is its own
        # business, and quietly relabelling a row is how a range answer ends up
        # attributed to the wrong system.
        canon = system_coords.get_name(sid)
        name = canon if (canon and canon.lower() == phrase.lower()) else phrase
        out.append(SystemRef(name, sid))
    return out


# ── sources (the staging list) ───────────────────────────────────────────────

GROUP_HOSTILE = "HOSTILE"
GROUP_FRIENDLY = "FRIENDLY"
GROUP_LINKED = "LINKED"
#: Render order. Hostiles first: "who can reach me" is a threat question.
GROUP_ORDER = (GROUP_HOSTILE, GROUP_FRIENDLY, GROUP_LINKED)


@dataclass(frozen=True)
class SourceLists:
    """The configured staging picture: the primary friendly staging plus both
    configured lists. The lists are what GROUPS a linked system; the primary is
    the only friendly source used when nothing is linked."""
    primary: SystemRef | None = None
    friendly: tuple = ()
    hostile: tuple = ()

    @property
    def configured(self) -> bool:
        return bool(self.primary or self.hostile or self.friendly)

    def default_sources(self) -> list:
        """``[(ref, group)]`` — primary friendly staging + every hostile staging.

        Grouped by CONSTRUCTION, not by ``group_of``: an owner whose staging
        system is not also listed under the friendly stagings would otherwise
        see their own staging rendered as an unaligned LINKED row."""
        out: list = []
        if self.primary is not None:
            out.append((self.primary, GROUP_FRIENDLY))
        out.extend((r, GROUP_HOSTILE) for r in self.hostile)
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
    try:
        sid = resolve(text) if resolve is not None else None
    except Exception:
        sid = None
    try:
        sid = int(sid)
    except (TypeError, ValueError):
        sid = None
    return SystemRef(text, sid if (sid or 0) > 0 else None)


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
    it can be rendered as a visible unresolved row rather than vanishing."""
    cfg = config if isinstance(config, dict) else {}
    zk = cfg.get("zkillboard")
    zk = zk if isinstance(zk, dict) else {}
    jr = cfg.get("jump_range")
    jr = jr if isinstance(jr, dict) else {}
    resolver = system_coords.resolve_name if resolve is None else resolve

    def _list(key):
        raw = jr.get(key)
        if not isinstance(raw, (list, tuple)):
            return ()
        refs = [_ref_of_name(n, resolver) for n in raw]
        return tuple(r for r in refs if r is not None)

    return SourceLists(
        primary=_ref_of_name(zk.get("staging_system"), resolver),
        friendly=_list("friendly_staging_systems"),
        hostile=_list("hostile_staging_systems"),
    )


# ── range maths ──────────────────────────────────────────────────────────────

#: The two hulls the summary answers for. ``Capital`` is the dreadnought range
#: (7.0 LY at JDC 5) — the hull that reaches where a titan (6.0) falls short,
#: which is the entire point of showing two columns.
HULL_TITAN = "Titan"
HULL_CAPITAL = "Dreadnought"
LABEL_TITAN = "Titan"
LABEL_CAPITAL = "Capital"
#: ``(display label, SHIP_RANGES key)`` in render order.
HULL_COLUMNS = ((LABEL_TITAN, HULL_TITAN), (LABEL_CAPITAL, HULL_CAPITAL))
#: Fallback if a hull key ever leaves ``SHIP_RANGES`` — never a silent 0 (which
#: would render "out of range" for everything, the dangerous direction).
_FALLBACK_RANGE_LY = 7.0


def hull_range_ly(hull) -> float:
    """JDC-5 max jump range for a hull, read LIVE off the CLASS dict
    ``jump_range.JumpRangeChecker.SHIP_RANGES``. Never a per-instance copy, and
    never a config value — see the module docstring for why that config key is
    deliberately dead here."""
    try:
        return float(jump_range.JumpRangeChecker.SHIP_RANGES[hull])
    except (KeyError, TypeError, ValueError):
        log.warning("range_check: no JDC-5 range for hull %r; using %.1f LY",
                    hull, _FALLBACK_RANGE_LY)
        return _FALLBACK_RANGE_LY


# ── the report ───────────────────────────────────────────────────────────────

ROW_OK = "ok"
ROW_UNRESOLVED = "unresolved"       # the NAME never resolved to a system
ROW_NO_DISTANCE = "no distance"     # resolved, but no distance was available

NOTE_TARGET_UNKNOWN = "Location unknown - no range answer."
NOTE_NO_SOURCES = "No staging systems configured."
TITLE_FALLBACK = "Range check"


@dataclass(frozen=True)
class RangeRow:
    """One source system's answer. ``distance_ly`` is the RAW distance — the
    in-range flags were computed from it unrounded; round only for display."""
    group: str
    name: str
    system_id: int | None
    distance_ly: float | None
    titan_in_range: bool
    capital_in_range: bool
    status: str = ROW_OK

    @property
    def ok(self) -> bool:
        return self.status == ROW_OK

    def in_range(self, hull) -> bool:
        return self.titan_in_range if hull == HULL_TITAN else self.capital_in_range


@dataclass(frozen=True)
class RangeReport:
    """What the toast renders. ``note`` is non-empty exactly when the answer is
    degraded, and is always plain ASCII (log-safe: this box's console is cp1252
    and a fancy glyph in a log line raises inside the stream handler)."""
    target: SystemRef | None = None
    rows: tuple = ()
    linked_used: bool = False
    note: str = ""

    @property
    def target_known(self) -> bool:
        return self.target is not None and self.target.resolved

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


def build_report(target, linked=(), *, sources=None, distance_fn=None) -> RangeReport:
    """Compute the range summary.

    ``target`` is the posting character's current system (a ``SystemRef``, a
    bare system id, or None when the location is unknown). ``linked`` is
    whatever ``extract_systems`` found in the message: **a non-empty ``linked``
    REPLACES the configured sources entirely** — the FC named the systems they
    want the answer for. ``sources`` is a ``SourceLists`` (from
    ``resolve_sources``); ``distance_fn(a_id, b_id) -> ly`` is injected
    (``jump_range.calculate_ly_distance`` in the app).

    Honesty rules, all three tested: an unknown target produces a report that
    SAYS the location is unknown rather than a plausible wrong answer; a source
    name that never resolved becomes an unresolved row; a resolved source with
    no available distance becomes a no-distance row. Rows are ordered
    hostiles-then-friendlies-then-linked, nearest first inside each group, with
    degraded rows last so they read as exceptions rather than as zero-distance.
    Pure."""
    srcs = sources if isinstance(sources, SourceLists) else SourceLists()
    target_ref = _as_ref(target)

    linked_refs = []
    for item in (linked or ()):
        ref = _as_ref(item)
        if ref is None or not (ref.name or ref.resolved):
            continue
        if not any(_same_system(ref, seen) for seen in linked_refs):
            linked_refs.append(ref)
    linked_used = bool(linked_refs)

    if linked_used:
        picked = [(ref, srcs.group_of(ref)) for ref in linked_refs]
    else:
        picked = []
        for ref, group in srcs.default_sources():
            if any(_same_system(ref, prev) for prev, _g in picked):
                continue
            picked.append((ref, group))

    if target_ref is None or not target_ref.resolved:
        return RangeReport(target=target_ref, rows=(), linked_used=linked_used,
                           note=NOTE_TARGET_UNKNOWN)
    if not picked:
        return RangeReport(target=target_ref, rows=(), linked_used=linked_used,
                           note=NOTE_NO_SOURCES)

    titan_ly = hull_range_ly(HULL_TITAN)
    capital_ly = hull_range_ly(HULL_CAPITAL)

    rows: list[RangeRow] = []
    for ref, group in picked:
        if not ref.resolved:
            rows.append(RangeRow(group, ref.name, None, None, False, False,
                                 ROW_UNRESOLVED))
            continue
        dist = _distance(distance_fn, ref.system_id, target_ref.system_id)
        if dist is None:
            rows.append(RangeRow(group, ref.name, ref.system_id, None,
                                 False, False, ROW_NO_DISTANCE))
            continue
        rows.append(RangeRow(group, ref.name, ref.system_id, dist,
                             dist <= titan_ly, dist <= capital_ly, ROW_OK))

    order = {g: i for i, g in enumerate(GROUP_ORDER)}
    rows.sort(key=lambda r: (order.get(r.group, len(order)),
                             0 if r.ok else 1,
                             r.distance_ly if r.distance_ly is not None else 0.0))
    return RangeReport(target=target_ref, rows=tuple(rows),
                       linked_used=linked_used, note="")


# ── render helpers (pure) ────────────────────────────────────────────────────

#: GUI text only — these are not cp1252 and MUST never reach ``log.*``.
MARK_IN = "✓"       # check mark
MARK_OUT = "✕"      # multiplication x


def hull_cell(label, in_range) -> str:
    """``"Titan ✓"`` / ``"Titan ✕"``. The glyph carries the answer as well as
    the colour, so the toast is readable without relying on red/green alone."""
    return f"{label} {MARK_IN if in_range else MARK_OUT}"


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
    return ""


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
#: Size floor/ceiling in px (see ``RangeToast._measure`` on the unit).
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

        body = tk.Frame(inner, bg=BG_PANEL)
        body.pack(fill="both", expand=True, padx=8, pady=(4, 0))
        self._clickable.append(body)

        if report.note:
            self._label(body, report.note, FG_YELLOW).grid(
                row=0, column=0, columnspan=4, sticky="w")
        else:
            self._fill_rows(body, max_rows)

        self._label(inner, "click to dismiss", FG_DIM, size=7).pack(
            fill="x", padx=8, pady=(3, 4))

    def _fill_rows(self, body, max_rows):
        try:
            cap = max(1, int(max_rows))
        except (TypeError, ValueError):
            cap = MAX_ROWS
        shown = 0
        line = 0
        hidden = 0
        for group, rows in self.report.groups():
            room = cap - shown
            if room <= 0:
                hidden += len(rows)
                continue
            self._label(body, group, GROUP_COLORS.get(group, FG_DIM),
                        bold=True).grid(row=line, column=0, columnspan=4,
                                        sticky="w", pady=(2, 0))
            line += 1
            for row in rows[:room]:
                self._grid_row(body, line, row)
                line += 1
                shown += 1
            hidden += max(0, len(rows) - room)
        if hidden:
            self._label(body, f"+{hidden} more", FG_DIM, size=7).grid(
                row=line, column=0, columnspan=4, sticky="w")

    def _grid_row(self, body, line, row):
        self._label(body, row.name, FG_TEXT).grid(
            row=line, column=0, sticky="w", padx=(6, 10))
        if row.ok:
            for col, (label, hull) in enumerate(HULL_COLUMNS, start=1):
                good = row.in_range(hull)
                self._label(body, hull_cell(label, good),
                            FG_GREEN if good else FG_RED).grid(
                    row=line, column=col, sticky="w", padx=(0, 10))
            self._label(body, format_distance(row.distance_ly), FG_DIM).grid(
                row=line, column=3, sticky="w")
        else:
            # Never render a flag we do not have: a degraded row says why, so a
            # missing answer can't read as "out of range" (or as "in range").
            self._label(body, row_note(row), FG_YELLOW).grid(
                row=line, column=1, columnspan=3, sticky="w")

    def _measure(self):
        """Content size in px, floored/capped.

        Tk lays out in logical px while ``set_window_pos`` takes physical px;
        the shipped ``ClientToast`` has the same one-unit assumption (its fixed
        430x78 box is treated as physical while its labels are laid out by Tk).
        Measuring keeps the two in the same relationship rather than hard-coding
        a box the content may not fit in."""
        try:
            self.top.update_idletasks()
            w = int(self.top.winfo_reqwidth())
            h = int(self.top.winfo_reqheight())
        except tk.TclError:                                # pragma: no cover
            return MIN_W, MIN_H
        return (max(MIN_W, min(w + 2, MAX_W)), max(MIN_H, min(h + 2, MAX_H)))

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
