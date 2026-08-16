"""
Battle LEDGER — ships and ISK lost/killed for a fight the user is actually in.

NOT a live tally, and the module is built so a renderer cannot accidentally
present it as one. Killmails reach zKillboard only through CCP's
``/characters|corporations/{id}/killmails/recent`` endpoints, both cached
300 s, which is the whole explanation for the measured arrival lag (median
~120 s, p90 267-281 s, FLAT across fight sizes). Nothing FCTool does moves
that floor. A T-2min number is useless for "primary the logi" but it is the
honest answer to "are we winning the trade" — a multi-minute question the FC
cannot answer in-game at all without alt-tabbing to a battle report.

THE FIVE HONESTY RULES, and where each is ENFORCED (in this module, not left
to the renderer):

 1. Stamp the DATA's clock, never the wall clock. ``confirmed_through`` is the
    newest ingested ``killmail_time`` for this fight; ``stamp_line`` renders it
    as ``confirmed through 14:32:10 (2m 05s behind)``. There is no code path
    that produces an "updated Ns ago" string.
 2. Never label it live. The state is IDLE / FILLING / SETTLED and every count
    is a FLOOR, because counts only ever go up. The floor is stated in WORDS
    (the ``NOTE_LAG`` note, the panel's state tooltip) rather than by prefixing
    every number: ``BattleLedgerView.floor_prefix`` is "" as of 2026-07-28 —
    owner directive, a ">=" on every row read as visual noise. The MEANING is
    unchanged and still machine-checked (``counts_are_floors`` stays True and
    ``NOTE_LAG`` is always emitted); only the glyph went. The seam is kept so a
    renderer still asks the engine rather than typing a prefix of its own.
    ``assert_no_live_language`` runs over every LAGGED string the
    view carries — ``title``, ``stamp_line``, ``state_label``, ``lag_line``,
    ``killed_note``, every row ``label``/``qualifier`` and every note — so a
    future edit that types "live", "updated" or "fresh" raises instead of
    shipping. The ONE deliberate exemption is ``BattleLedgerView.fast``
    (``FastStrip.text`` / ``latency_label``): that lane really IS fast and
    carries its own latency label, so freshness language there is honest.
 3. Never animate the lagging numbers as events. The view is a plain immutable
    snapshot with a ``revision`` counter; it carries no event/flash/ping field
    for a renderer to hook, and ``BattleLedgerPanel`` re-``config``s labels
    rather than flashing them.
 4. The lagged ledger and any fast signal stay STRUCTURALLY distinct.
    ``BattleLedgerView.rows`` (uniform zKill provenance, one shared clock) and
    ``BattleLedgerView.fast`` (a separate ``FastStrip`` carrying its OWN
    latency label) are different types in different fields, and
    ``render_model`` asserts every row shares one provenance. A consumer
    cannot merge them by accident. This is the constraint the spec missed:
    a 25-second Ours row beside a 2-minute Enemies row makes an FC conclude
    they are losing badly when the enemy's losses simply have not landed yet.
    MIXED LATENCY INSIDE ONE COMPARISON IS WORSE THAN UNIFORM LAG.
 5. Name the blind spots instead of rendering a misleading zero. A bucket that
    cannot be attributed carries ``ships=None`` — not 0 — so the renderer has
    no zero to draw, plus a ``LedgerNote`` saying why.

Purity: no Tk, no fc_gui import, no network, no app state. Everything the
engine needs from the app arrives through injected callables/setters
(``presence_provider``, ``set_fleet_roster``, ``set_standings``,
``resolve_system_name``, ``resolve_ship_group``), so the wiring bite is a
handful of lines. The Tk panel lives in the sibling ``battle_ledger_panel``
module.

Threading: ``ZKillMonitor`` calls ``ingest`` on its poll thread while the Tk
thread calls ``tick``/``render_model``, so every public method takes an
internal lock and ``render_model`` returns an immutable snapshot. Nothing here
touches a widget — see ``battle_ledger_panel.BattleLedgerPanel.update_view``
for the UI-thread contract.
"""

from __future__ import annotations

import re
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterable

from loss_tracker import CAPSULE_TYPE_IDS
from zkill_monitor import MAX_KILL_AGE, is_kill_stale

# ── Ownership buckets ───────────────────────────────────────────────────────
# Deliberately DUPLICATED from loss_reconciler.BUCKET_* / standings_cache.
# BUCKET_* rather than imported, following the precedent standings_cache set
# for exactly these three strings: this module must not pull the loss-tracking
# engine in behind it. tests/test_battle_ledger.py carries the drift guard that
# asserts all three definitions stay identical.
BUCKET_OURS = "ours"
BUCKET_ALLIES = "allies"
BUCKET_ENEMIES = "enemies"
BUCKET_ORDER = (BUCKET_OURS, BUCKET_ALLIES, BUCKET_ENEMIES)
BUCKET_LABELS = {
    BUCKET_OURS: "Ours",
    BUCKET_ALLIES: "Allies",
    BUCKET_ENEMIES: "Enemies",
}

# ── Provenance ──────────────────────────────────────────────────────────────
# Every row in the bucket table MUST carry this one value. It exists so the
# uniformity assertion has something to compare; a second provenance string
# appearing in `rows` is the mixed-latency bug, and render_model raises on it.
PROVENANCE_ZKILL = "zkill"

# ── Arrival latency: the number every timing constant below is derived from ──
# Goal 3 measurement, re-derived in the Goal 4 spike: median ~120 s, p90
# 267-281 s, flat across every fight size, floored by CCP's 300 s killmail
# cache. These two are documentation, not tuning knobs.
ZKILL_P90_ARRIVAL_S = 281
CCP_KILLMAIL_CACHE_S = 300

# QUIET_S — the single most load-bearing number in this module.
#
# The ledger declares a fight SETTLED after this long with no qualifying
# killmail ARRIVING. Arrival is lagged with a FLAT p90 of 267-281 s and a hard
# structural ceiling of CCP's 300 s cache, so any quiet window shorter than
# ~300 s declares the fight over WHILE ITS OWN TAIL IS STILL LANDING: the panel
# would stamp SETTLED and then keep receiving kills for the fight it just
# called finished. 360 s is the smallest round value that clears both the
# measured p90 (+79 s of headroom) and the cache ceiling (+60 s).
#
# Direction of error matters and is asymmetric:
#   too LOW  -> a wrong SETTLED label on a fight that is still resolving. The
#               harm is real: an FC reading "SETTLED, we traded even" while the
#               enemy's losses are still in flight.
#   too HIGH -> the SETTLED label just arrives later. Nothing is lost.
# So when in doubt, raise it. It is also why SETTLED -> FILLING is a LEGAL
# transition here (see _LEGAL_TRANSITIONS): even past the quiet window this
# ledger keeps ACCEPTING kills for its system and re-opens rather than dropping
# them. QUIET_S is a labelling threshold, never a data cutoff.
QUIET_S = 360

# A single gank in your system must not open a battle panel. Arming needs
# either a second qualifying killmail whose killmail_time is within
# ARM_WINDOW_S of the first (the DATA clock — this is a fact about the fight,
# not about when we heard), or one killmail with at least this many attackers.
ARM_WINDOW_S = 120
ARM_MIN_ATTACKERS = 10

# On arming, buffered qualifying kills for the same system are folded in
# retroactively (this is what makes a mid-fight FCTool restart recover its
# ledger instead of starting from zero). Bounded to one fight's worth of
# history so a kill from 25 minutes ago in the same system cannot be swept
# into an unrelated engagement.
FIGHT_FOLD_BACK_S = 900

# Presence (condition B) freshness, for the `own_presence` registry the wiring
# bite feeds from the existing location polls. Long enough to survive the map
# tab being hidden or previews being off; short enough that a stale entry
# cannot arm a ledger in a system you have already left. The registry applies
# it — `presence_provider` hands this module system ids that are ALREADY
# fresh — but the number lives here because it belongs to this design.
PRESENCE_TTL_S = 180

# Your accounts left the field: settle (do not clear — the FC most wants to
# read the final ledger AFTER extracting).
PRESENCE_LOST_SETTLE_S = 120

# A settled ledger clears itself this long after settling, so a stale battle
# report does not sit on the tab forever. Comfortably past any arrival tail.
CLEAR_AFTER_SETTLED_S = 900

# Bound on the pre-arm / cross-system candidate buffer. Only killmails that
# already passed BOTH qualification tests reach it (our group participating,
# our accounts present), so it cannot grow with the K-space firehose — but it
# is capped anyway, because the adjacent EngagementTracker._kills leak
# (zkill_monitor.py) is exactly the pattern not to repeat.
REPLAY_BUFFER_MAX = 500

# zKillboard's feed is K-space only (ZKillMonitor.watch_all accepts
# 30000000-30999999); wormhole systems are 31000000+. A J-space fight produces
# NO killmails on this path, so the honest answer is a named blind spot, never
# an empty table that reads as "no losses".
KSPACE_MIN_SYSTEM_ID = 30000000
KSPACE_MAX_SYSTEM_ID = 30999999
JSPACE_MIN_SYSTEM_ID = 31000000

# ── States ──────────────────────────────────────────────────────────────────
STATE_IDLE = "idle"        # no fight; the panel is hidden (or blind-noticed)
STATE_FILLING = "filling"  # kills still arriving; every count is a floor
STATE_SETTLED = "settled"  # quiet past QUIET_S, or our accounts left the field

# Illegal transitions are impossible by construction: _transition validates
# against this table and raises. Note IDLE -> SETTLED is absent (a ledger with
# no fight cannot settle) and SETTLED -> FILLING is present (see QUIET_S).
_LEGAL_TRANSITIONS: dict[str, frozenset[str]] = {
    STATE_IDLE: frozenset({STATE_FILLING}),
    STATE_FILLING: frozenset({STATE_SETTLED, STATE_IDLE}),
    STATE_SETTLED: frozenset({STATE_FILLING, STATE_IDLE}),
}

STATE_LABELS = {
    STATE_IDLE: "NO FEED",
    STATE_FILLING: "FILLING",
    STATE_SETTLED: "SETTLED",
}


class IllegalTransition(RuntimeError):
    """A state change the ledger's machine does not permit was attempted."""


# ── Ingest vocabulary ───────────────────────────────────────────────────────
ACTION_COUNTED = "counted"    # folded into the live ledger
ACTION_BUFFERED = "buffered"  # qualified, but not (yet) part of a live fight
ACTION_IGNORED = "ignored"    # see `reason`

REASON_OK = ""
REASON_DISABLED = "ledger_disabled"        # the OWNER turned the feature off
REASON_FEED_DISABLED = "feed_disabled"     # zKill feed off; the ledger is on
REASON_UNPARSEABLE = "unparseable"
REASON_NO_TIME = "no_killmail_time"
REASON_STALE = "stale"                     # older than MAX_KILL_AGE
REASON_OUT_OF_KSPACE = "out_of_kspace"     # J-space / abyssal / unknown id
REASON_NOT_PARTICIPATING = "not_participating"
REASON_NO_PRESENCE = "no_account_in_system"
REASON_DUPLICATE = "duplicate_killmail"
REASON_STARTUP_REPLAY = "startup_replay"   # predates this ledger; may not ARM
REASON_AWAITING_ARM = "awaiting_arm"       # one kill is not a battle
REASON_OTHER_SYSTEM = "other_system"       # a fight is live somewhere else

# ── Blind-spot note kinds (honesty rule 5) ──────────────────────────────────
NOTE_FEED_DISABLED = "feed_disabled"
NOTE_JSPACE = "jspace"
NOTE_STANDINGS_UNKNOWN = "standings_unknown"
NOTE_NO_ATTRIBUTION = "no_attribution"
NOTE_LAG = "lag"
NOTE_PODS = "pods"
NOTE_STRUCTURES = "structures"

_NOTE_TEXT = {
    NOTE_FEED_DISABLED: "zKill feed is off — no kill data at all, not zero kills.",
    NOTE_JSPACE: "zKill does not cover J-space — a wormhole fight is invisible here.",
    NOTE_STANDINGS_UNKNOWN: (
        "Standings cache not built — blues cannot be told from reds, so only "
        "fleet-roster losses are attributable."),
    NOTE_NO_ATTRIBUTION: (
        "No fleet roster and no standings — nothing can be attributed to a side."),
}

# ── The floor marker (honesty rule 2, as a glyph) ───────────────────────────
# Prefixed to every count a renderer draws. EMPTY since 2026-07-28 by owner
# directive: a ">=" in front of all three rows read as noise on a panel whose
# every other line already says the same thing. Deliberately still a seam
# rather than a deletion — `NOTE_LAG` ("Counts are floors — ... numbers only
# ever go up") is emitted on EVERY render and the panel's state tooltip repeats
# it, so the claim is carried in words, where it is also machine-checkable.
# Restoring the glyph is a one-character edit here, in ONE place, and the panel
# picks it up without a change.
FLOOR_PREFIX = ""


# ── Live-language guard (honesty rule 2) ────────────────────────────────────
# Applied to every LAGGED string render_model puts in the view (title,
# stamp_line, state_label, lag_line, killed_note, each row's label and
# qualifier, every note) and to the panel's own static copy. A future edit that
# reintroduces "updated 3s ago", "latest losses" or a "LIVE" chip fails here
# instead of shipping a lie about freshness.
#
# BattleLedgerView.fast is the single deliberate exemption and is named as one
# rather than left out by omission: the fast lane IS fast and carries its own
# latency label, so "live" there would be true.
#
# Word boundaries throughout, so real words that merely CONTAIN a token pass:
# "known"/"now-retired" are not "now", "instantiate" is not "instant". Kept
# clear of bare `now` for the same reason. The bundled SDE system-name table
# (which reaches `title`) was scanned against this pattern — zero matches.
_LIVE_LANGUAGE = re.compile(
    r"\b(live|real[\s-]?time|realtime|up[\s-]?to[\s-]?date|"
    r"updat(?:e|es|ed|ing)|refresh(?:es|ed|ing)?|"
    r"fresh|latest|instant|streaming|"
    r"just\s+now|right\s+now|as\s+of\s+now|happening\s+now|"
    r"seconds?\s+ago|current(?:ly)?)\b",
    re.IGNORECASE,
)


class LiveLanguageError(AssertionError):
    """A view string claimed freshness the data does not have."""


def assert_no_live_language(text: str, where: str = "view") -> str:
    """Return `text`, or raise if it claims liveness the ledger cannot have.

    Public so the panel's own static copy — including its tooltips — can be
    checked by the same rule the engine holds itself to. NOT applied to
    `FastStrip` content: see the exemption note on `_LIVE_LANGUAGE`.
    """
    if text and _LIVE_LANGUAGE.search(text):
        raise LiveLanguageError(
            f"{where}: {text!r} claims freshness this data does not have — "
            "the ledger is lagged by ~2 minutes and must never read as live")
    return text


# ── datetime + formatting helpers ───────────────────────────────────────────
# The project carries MIXED aware/naive timestamps, so everything entering the
# arithmetic is normalised first (the _market_scan_ts class of bug).

def to_utc(value: datetime | None) -> datetime | None:
    """Normalise an IN-PROCESS datetime to aware UTC (a naive value is read as
    LOCAL, which is what datetime.now() produces)."""
    if value is None:
        return None
    return value.astimezone(timezone.utc)


def parse_killmail_time(value) -> datetime | None:
    """Parse a killmail's `killmail_time` to aware UTC, or None.

    A naive STRING is read as UTC (killmail times are UTC by ESI spec);
    contrast `to_utc`, which reads a naive datetime as local. Same convention
    as loss_reconciler.parse_killmail_time — the two must not drift.
    """
    if isinstance(value, datetime):
        return to_utc(value)
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except Exception:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def format_behind(seconds: float) -> str:
    """A duration as `45s` / `2m 05s` / `1h 02m`. Negative clamps to `0s`."""
    total = int(max(0, round(seconds)))
    if total < 60:
        return f"{total}s"
    if total < 3600:
        return f"{total // 60}m {total % 60:02d}s"
    return f"{total // 3600}h {(total % 3600) // 60:02d}m"


def format_isk(value: float | None) -> str:
    """Compact ISK: `1.2B` / `340.0M` / `4.5K` / `900`. None -> `--`.

    Mirrors FCToolGUI._market_price_short so the app reads one way; the panel
    takes an injected formatter so the wiring bite can pass that method
    directly and keep exactly one implementation.
    """
    if value is None:
        return "--"
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return "--"
    if amount >= 1_000_000_000:
        return f"{amount / 1_000_000_000:.1f}B"
    if amount >= 1_000_000:
        return f"{amount / 1_000_000:.1f}M"
    if amount >= 1_000:
        return f"{amount / 1_000:.1f}K"
    return f"{amount:.0f}"


# ── Composition (what the ships in a bucket actually were) ──────────────────
# "3 Battleship, 2 Logistics, 1 Dreadnought" — the answer to the question the
# bare ship count cannot answer ("we lost 6" vs "we lost 6 logi"). Capped
# because a 60-hull brawl would otherwise produce a tooltip taller than the
# screen; the tail is SUMMARISED, never silently dropped.
COMPOSITION_MAX_ENTRIES = 5


@dataclass(frozen=True)
class CompositionEntry:
    """One class of hull inside a bucket.

    `name` is the SDE ship-GROUP name ("Battleship", "Logistics") as returned
    by the injected `resolve_ship_group`, or "" when the group could not be
    resolved — an empty name is honest, not a bug: an unmapped or brand-new
    hull is still a ship that died, and dropping it would understate the count
    the row already shows. `format_composition` renders it as plain "N ships".
    """
    name: str
    count: int


def _composition_sort_key(entry: CompositionEntry):
    """Descending by count, then alphabetical; the UNNAMED entry always last.

    Sorting lives with the formatter as well as the builder so the cap can
    never drop the biggest group just because a caller handed over an unsorted
    tuple.
    """
    return (1 if not entry.name else 0, -entry.count, entry.name)


def _ships_phrase(count: int) -> str:
    return f"{count} ship{'s' if count != 1 else ''}"


def format_composition(entries: Iterable[CompositionEntry],
                       *, max_entries: int = COMPOSITION_MAX_ENTRIES) -> str:
    """`3 Battleship, 2 Logistics, 1 Dreadnought`, or "" for nothing at all.

    ASCII only (this string reaches a Tk tooltip AND, via the same wording, the
    log — see the cp1252 rule). Descending by count. Past `max_entries` the
    remainder collapses to `+N more ships`, so the number of hulls named in the
    line always reconciles with the bucket's own count.
    """
    items = sorted((e for e in (entries or ()) if e.count > 0),
                   key=_composition_sort_key)
    if not items:
        return ""
    limit = max(1, int(max_entries))
    shown, rest = items[:limit], items[limit:]
    parts = [f"{e.count} {e.name}" if e.name else _ships_phrase(e.count)
             for e in shown]
    remainder = sum(e.count for e in rest)
    if remainder:
        # "+7 more ships", not a bare "+7": every other number on this line is
        # a hull count, and the tail has to read as one too.
        parts.append(f"+{remainder} more ship{'s' if remainder != 1 else ''}")
    return ", ".join(parts)


# ── Battle report link ──────────────────────────────────────────────────────
# Same pattern zkill_monitor.py already posts in its alerts, kept as a pure
# string builder here so the engine stays network-free and the panel stays
# browser-free (it takes an injected `on_open_url`).
WARBEACON_BR_URL = "https://warbeacon.net/br/related/{system_id}/{stamp}/"


def warbeacon_url(system_id, when: datetime | None) -> str:
    """WarBeacon's related-kills battle report for `system_id` at `when`.

    `when` is the DATA clock — the fight's FIRST killmail_time — never
    `datetime.now()`. The stamp selects the window the report covers, so
    anchoring it on the wall clock would open a report that starts partway
    through a long fight (and, on a SETTLED ledger the FC is reading ten
    minutes later, one that starts after it ended). Normalised to UTC first:
    the project carries MIXED aware/naive timestamps.

    Returns "" — never a half-built URL — when there is no system or no clock
    to stamp, so a caller can treat truthiness as "there is a report to open".
    """
    try:
        sid = int(system_id)
    except (TypeError, ValueError):
        return ""
    stamped = to_utc(when)
    if not sid or stamped is None:
        return ""
    return WARBEACON_BR_URL.format(system_id=sid,
                                   stamp=stamped.strftime("%Y%m%d%H%M"))


def format_stamp_line(confirmed_through: datetime | None,
                      now: datetime | None = None) -> str:
    """Honesty rule 1, stated once.

    `confirmed through 14:32:10 (2m 05s behind)` — the DATA's own clock plus
    how far behind it is. There is deliberately no code path in this module
    that formats a wall-clock "updated N seconds ago": that would be a lie
    about freshness, and it is the single thing this panel must never say.
    """
    if confirmed_through is None:
        return "nothing confirmed yet"
    stamped = to_utc(confirmed_through)
    reference = to_utc(now) or datetime.now(timezone.utc)
    behind = (reference - stamped).total_seconds()
    return (f"confirmed through {stamped.strftime('%H:%M:%S')} "
            f"({format_behind(behind)} behind)")


# ── Killmail facts ──────────────────────────────────────────────────────────

def _as_int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def inner_killmail(kill_data: dict) -> dict | None:
    """Unwrap whichever killmail envelope we were handed.

    Handles the internal normalized shape ({"killmail": ..., "zkb": ...}), the
    raw R2Z2 shape ({"esi": ..., "zkb": ...}) and a bare killmail dict —
    mirroring ZKillMonitor._normalize_kill, which the production hook has
    already applied.
    """
    if not isinstance(kill_data, dict):
        return None
    for key in ("killmail", "esi"):
        nested = kill_data.get(key)
        if isinstance(nested, dict):
            return nested
    return kill_data


@dataclass(frozen=True)
class KillFacts:
    """Everything the ledger needs from one killmail.

    Wider than loss_reconciler.Killmail on purpose: the participation test
    (condition A) reads the ATTACKER side too, which loss reconciliation never
    needs. `time` is AWARE UTC (or None). `isk_value` is `zkb.totalValue`.
    """
    killmail_id: int | None
    system_id: int
    time: datetime | None
    isk_value: float
    victim_character_id: int
    victim_corporation_id: int
    victim_alliance_id: int
    victim_ship_type_id: int
    attacker_count: int
    attacker_entity_ids: frozenset  # corp + alliance ids across all attackers
    attacker_character_ids: frozenset

    @property
    def is_capsule(self) -> bool:
        """SHIP+POD DEDUP, half one. One dead pilot emits TWO killmails; the
        capsule must never add to the ship count. Same predicate and same
        CAPSULE_TYPE_IDS source as loss_reconciler."""
        return self.victim_ship_type_id in CAPSULE_TYPE_IDS

    @property
    def is_structure_or_npc(self) -> bool:
        """No victim character = a structure, citadel or NPC. A dead Astrahus
        is not a "ship lost"; it is counted on its own line."""
        return not self.victim_character_id

    @property
    def in_kspace(self) -> bool:
        return KSPACE_MIN_SYSTEM_ID <= self.system_id <= KSPACE_MAX_SYSTEM_ID


def parse_kill(kill_data: dict) -> KillFacts | None:
    """Extract `KillFacts` from any killmail envelope, or None if it is not a
    dict at all. Never raises on malformed input."""
    nested = inner_killmail(kill_data)
    if nested is None:
        return None
    victim = nested.get("victim")
    if not isinstance(victim, dict):
        victim = {}
    zkb = kill_data.get("zkb") if isinstance(kill_data, dict) else None
    if not isinstance(zkb, dict):
        zkb = {}
    attackers = nested.get("attackers")
    if not isinstance(attackers, list):
        attackers = []
    entity_ids: set[int] = set()
    char_ids: set[int] = set()
    for attacker in attackers:
        if not isinstance(attacker, dict):
            continue
        for key in ("corporation_id", "alliance_id"):
            value = _as_int(attacker.get(key))
            if value:
                entity_ids.add(value)
        value = _as_int(attacker.get("character_id"))
        if value:
            char_ids.add(value)
    try:
        isk_value = float(zkb.get("totalValue") or 0.0)
    except (TypeError, ValueError):
        isk_value = 0.0
    killmail_id = nested.get("killmail_id", kill_data.get("killmail_id"))
    return KillFacts(
        killmail_id=_as_int(killmail_id) or None,
        system_id=_as_int(nested.get("solar_system_id")),
        time=parse_killmail_time(nested.get("killmail_time")),
        isk_value=isk_value,
        victim_character_id=_as_int(victim.get("character_id")),
        victim_corporation_id=_as_int(victim.get("corporation_id")),
        victim_alliance_id=_as_int(victim.get("alliance_id")),
        victim_ship_type_id=_as_int(victim.get("ship_type_id")),
        attacker_count=len(attackers),
        attacker_entity_ids=frozenset(entity_ids),
        attacker_character_ids=frozenset(char_ids),
    )


def classify_victim(facts: KillFacts, *, fleet_char_ids=frozenset(),
                    own_ids=frozenset(), ally_ids=frozenset()) -> str:
    """Bucket a killmail's VICTIM as ours / allies / enemies.

    Precedence is fixed and matches loss_reconciler.classify_victim and
    standings_cache.bucket_of: fleet roster -> own corp/alliance -> blues ->
    everyone else. This is a three-way partition of everything, so "what we
    killed" needs no separate column: it is the Enemies row, and no killmail is
    ever counted twice.
    """
    if facts.victim_character_id and facts.victim_character_id in fleet_char_ids:
        return BUCKET_OURS
    for entity_id in (facts.victim_corporation_id, facts.victim_alliance_id):
        if entity_id and entity_id in own_ids:
            return BUCKET_OURS
    # The blue test includes the victim's own CHARACTER id, because a blue list
    # is not only corps and alliances: personal contacts are individual pilots.
    # On the owner's live cache (2026-08-16) roughly 14 of 56 ally ids look like
    # character ids by id-range inference, so a corp/alliance-only test silently
    # files about a quarter of a real blue list under Enemies. The character id
    # is tested AFTER the roster and own-id checks above,
    # so precedence is unchanged; `standings_cache.is_friendly` has always
    # tested char_id, and `bucket_of` was brought into step in the same change.
    for entity_id in (facts.victim_corporation_id, facts.victim_alliance_id,
                      facts.victim_character_id):
        if entity_id and entity_id in ally_ids:
            return BUCKET_ALLIES
    return BUCKET_ENEMIES


def participates(facts: KillFacts, *, fleet_char_ids=frozenset(),
                 own_ids=frozenset(), ally_ids=frozenset(),
                 armed=False) -> bool:
    """Condition A — "the user's group is participating" — as the UNION of
    three tests, per the research plus the 2026-08-16 ally fix:

      * standings: any victim/attacker corp or alliance id in `own_ids`;
      * roster:    any victim/attacker character id on the live fleet roster;
      * allies:    the VICTIM is blue (corp, alliance or character), and the
                   caller passes `armed=True` because our own participation has
                   already opened a ledger for this fight.

    The first two matter in both directions. The roster test catches
    neutral-corp alts the standings test misses, and it is the STRONGER signal
    — but it is boss-only (ESI 403s on /fleets/{id}/members/ otherwise). The
    standings test works when you are not fleet boss.

    THE ALLY TERM, and why it is this narrow (the bug it fixes: without it a
    blue dying to hostiles matched nothing, so it was discarded HERE and
    `classify_victim`'s BUCKET_ALLIES branch was reachable only through
    blue-on-blue friendly fire — the owner's report that the ledger "never
    seems to pick up allied losses, only friendly"):

      * VICTIM only, never an ally on the guns. Blues shoot things all over
        K-space; qualifying on an ally ATTACKER would pull in fights we are
        merely near. The kills we forgo by that choice would have landed in the
        Enemies row, so the error runs toward "we killed less than we did",
        which is the un-flattering direction — the one honesty rule 5 exists to
        keep this module pointed at.
      * ARMED only. Allies may be counted INTO a fight our own group armed;
        an all-blue brawl must never ARM a ledger we are not in, or one
        character parked in a system would open a battle panel for somebody
        else's fight.

    The same gate drops an ally loss that ARRIVES before the arming killmail
    (the caller does not buffer it either: the candidate buffer feeds the
    two-kill arm test, so buffering blue-on-blue mails would let them supply
    the arm witness through the back door). That under-counts the Allies row at
    the very start of a fight, which is the conservative direction.
    """
    if own_ids:
        for entity_id in (facts.victim_corporation_id, facts.victim_alliance_id):
            if entity_id and entity_id in own_ids:
                return True
        if facts.attacker_entity_ids & own_ids:
            return True
    if fleet_char_ids:
        if facts.victim_character_id and facts.victim_character_id in fleet_char_ids:
            return True
        if facts.attacker_character_ids & fleet_char_ids:
            return True
    if armed and ally_ids:
        for entity_id in (facts.victim_corporation_id, facts.victim_alliance_id,
                          facts.victim_character_id):
            if entity_id and entity_id in ally_ids:
                return True
    return False


# ── Render model (what the panel draws — a pure data structure) ─────────────

@dataclass(frozen=True)
class LedgerRow:
    """One bucket's line in the table.

    `ships` / `isk` / `pods` are None — NOT 0 — when the bucket cannot be
    attributed. That is honesty rule 5 enforced in the type system: a renderer
    handed None has no zero to draw, and must show the blank marker instead.

    `provenance` exists so `render_model` can assert every row shares one, and
    is therefore the mechanical guard against the mixed-latency table
    (a fast Ours row beside a lagged Enemies row) that the research identified
    as the single worst thing this feature could do.
    """
    bucket: str
    label: str
    ships: int | None
    isk: float | None
    pods: int | None
    provenance: str = PROVENANCE_ZKILL
    qualifier: str = ""      # e.g. "roster only" — why this row is narrower
    # WHAT the ships were, already resolved to group names: a tuple of
    # CompositionEntry, descending, unnamed last. A tuple rather than a
    # Counter/dict on purpose — LedgerRow is a frozen dataclass whose `==` the
    # panel diffs on every repaint, and an unhashable field would also break
    # the generated __hash__. Empty for a suppressed bucket: a row that cannot
    # be attributed has no composition either (honesty rule 5 extends here —
    # naming hulls under a blanked row would leak exactly the number the row
    # is refusing to state).
    composition: tuple = ()

    @property
    def suppressed(self) -> bool:
        return self.ships is None


@dataclass(frozen=True)
class LedgerNote:
    """A named blind spot or caveat. `kind` is one of the NOTE_* constants."""
    kind: str
    text: str


@dataclass(frozen=True)
class FastStrip:
    """The fast lane — deliberately a DIFFERENT type in a DIFFERENT field from
    `BattleLedgerView.rows` (honesty rule 4).

    Fast signals exist only for OURS (own-account damage/pod checks, the
    fleet-loss percentage). Putting any of them in the bucket table would
    invite a false comparison. So they live here, carrying their own latency
    label, and the panel renders them in a visually separate strip. Nothing in
    this module ever copies a value between `rows` and `fast`.
    """
    text: str
    latency_label: str


@dataclass(frozen=True)
class BattleLedgerView:
    """Everything the panel draws, and nothing else. Immutable snapshot.

    Carries no event/flash/animation field on purpose (honesty rule 3): a
    number that jumps two minutes after the fact must not read as "it just
    happened", so there is nothing here for a renderer to animate off.
    """
    visible: bool
    state: str
    state_label: str
    title: str
    system_id: int | None
    system_name: str
    confirmed_through: datetime | None
    stamp_line: str
    lag_line: str
    floor_prefix: str
    rows: tuple
    notes: tuple
    fast: FastStrip | None
    killed_note: str
    revision: int
    # The WarBeacon battle report for this fight, or "" when there is nothing
    # to link. Built here (from the fight's FIRST killmail_time) rather than in
    # the panel so the panel needs no clock and no URL grammar of its own.
    battle_report_url: str = ""

    @property
    def counts_are_floors(self) -> bool:
        """Always True — and STILL true now that `floor_prefix` is "".

        Counts only ever go up: a bucket's number is the number of kills that
        have ARRIVED, never the number that happened. Dropping the ">=" glyph
        (owner directive 2026-07-28) changed how the floor is SAID, not whether
        it holds; the NOTE_LAG note and the state tooltip say it in words on
        every render.
        """
        return True

    def row(self, bucket: str) -> LedgerRow | None:
        for entry in self.rows:
            if entry.bucket == bucket:
                return entry
        return None


# ── Internal accumulators ───────────────────────────────────────────────────

@dataclass
class _Totals:
    ships: int = 0
    isk: float = 0.0
    pods: int = 0
    pod_isk: float = 0.0
    structures: int = 0
    structure_isk: float = 0.0
    # victim ship_type_id -> how many died. Ships ONLY: pods and structures
    # keep their own counters (and their own lines), so the composition always
    # reconciles with `ships` and never claims a capsule was a hull. Resolution
    # to group names is deferred to render time — the type id is what the
    # killmail carries, and the resolver is injected.
    ship_types: Counter = field(default_factory=Counter)


@dataclass
class _Candidate:
    """A qualifying killmail held outside the live ledger.

    Two populations share this buffer: kills that predate the ledger (the
    ~30-minute startup replay, which may be COUNTED once a fight arms but may
    never ARM one), and kills that qualified while no fight was armed for their
    system (a lone gank waiting to see whether it becomes a battle).
    """
    facts: KillFacts
    replay: bool


class BattleLedger:
    """Fight detection + per-bucket accounting. Pure: no Tk, no network.

    Wiring, all of it injection — this class never reaches into app state:

        ledger = battle_ledger.from_config(
            self.config,
            presence_provider=self._own_presence_system_ids,
            resolve_system_name=system_coords.get_name,   # OFFLINE, see below
            resolve_ship_group=self._ledger_ship_group,   # OFFLINE, see below
        )
        zkill_monitor.on_killmail = ledger.ingest        # zKill poll thread
        ...
        ledger.set_fleet_roster(m["character_id"] for m in members)
        ledger.set_standings(own_ids=cache.own_ids, ally_ids=cache.ally_ids,
                             identity_known=cache.own_identity_known)
        ledger.set_feed_enabled(self.zkill_monitor is not None)

    and on the Tk side, at <= 1 Hz:

        ledger.tick()
        panel.update_view(ledger.render_model())

    NOT `if ledger.tick(): ...` — `tick` reports only the timer-driven state
    changes it made, so an ingest between two ticks would never reach the
    screen. `update_view` is the thing that short-circuits: it diffs the whole
    view and returns without touching a widget when nothing moved. Diffing the
    view rather than `revision` is also what keeps the "(2m 05s behind)" figure
    counting up while the counts sit still.

    EVERY INJECTED CALLABLE MUST BE NON-BLOCKING — a hard contract, not a
    preference. Unlike `loss_reconciler.ingest`, which DROPS its lock precisely
    so it can resolve names over the wire, this engine holds `self._lock` ACROSS
    its injected callables: `render_model` -> `_system_name` calls
    `resolve_system_name` inside the lock, on the Tk thread, at 1 Hz, while the
    zKill poll thread may be sitting in `ingest` behind that same lock. So
    `resolve_system_name` must be a pure/offline lookup —
    `system_coords.get_name` reads the BUNDLED SDE table and is the reference
    implementation. It must NOT be `zkill_monitor.resolve_name`, which does
    `rate_limit("esi")` + `requests.get(timeout=5)` on a cache miss: that
    reproduces the 1.15 s Tk-thread stall class `system_coords.get_name` was
    adopted to prevent, and stalls the poll thread along with it. The same rule
    binds `presence_provider` (a local dict read — see `_presence_systems`),
    `resolve_ship_group` (a bundled-table dict read, and it is called once per
    DISTINCT hull type per render, so a wire call there costs one stall per
    ship class in the fight) and the `isk_format` the panel takes.

    TWO CLOCKS, deliberately kept apart:
      * the DATA clock (killmail_time, aware UTC) drives `confirmed_through`
        and the staleness string — what the FC reads;
      * the ARRIVAL clock (monotonic) drives the quiet window and the
        presence-loss timer — "no kill has REACHED us for QUIET_S".
    Driving the quiet window off killmail_time would settle every fight
    instantly, because every kill is already ~2 minutes old on arrival. Using
    arrival also means the effective coverage after a fight truly ends is
    arrival_lag + QUIET_S (~8-10 minutes), which errs in the safe direction.
    """

    def __init__(self, *,
                 presence_provider: Callable[[], Iterable[int]] | None = None,
                 resolve_system_name: Callable[[int], str] | None = None,
                 resolve_ship_group: Callable[[int], str | None] | None = None,
                 quiet_s: float = QUIET_S,
                 arm_window_s: float = ARM_WINDOW_S,
                 arm_min_attackers: int = ARM_MIN_ATTACKERS,
                 presence_lost_settle_s: float = PRESENCE_LOST_SETTLE_S,
                 clear_after_settled_s: float = CLEAR_AFTER_SETTLED_S,
                 fold_back_s: float = FIGHT_FOLD_BACK_S,
                 max_kill_age: timedelta = MAX_KILL_AGE,
                 started_at: datetime | None = None,
                 count_pod_isk: bool = False,
                 jspace_notice: bool = True,
                 enabled: bool = True,
                 feed_enabled: bool = True,
                 own_ids: Iterable[int] | None = None,
                 ally_ids: Iterable[int] | None = None,
                 identity_known: bool = False,
                 utcnow: Callable[[], datetime] | None = None,
                 monotonic: Callable[[], float] | None = None):
        self._lock = threading.RLock()
        # NON-BLOCKING CONTRACT (see the class docstring): all three of these
        # are invoked with `self._lock` HELD — `presence_provider` from `ingest`
        # on the zKill poll thread, `resolve_system_name` and
        # `resolve_ship_group` from `render_model` on the Tk thread at 1 Hz.
        # Anything that can do network I/O here stalls the UI and the poll
        # thread together. Pass `system_coords.get_name`, never an ESI resolver;
        # and for the ship group pass an OFFLINE lookup over the bundled type
        # table (`type_catalog.SHIP_GROUP_NAMES` keyed off the bundled
        # fit_types entry's group id) with the TypeCatalog ESI fallback
        # DISABLED — an unmapped hull must degrade to "" and be rendered as a
        # plain "N ships", never reach for the wire under the lock.
        self._presence_provider = presence_provider
        self._resolve_system_name = resolve_system_name
        self._resolve_ship_group = resolve_ship_group
        self._quiet_s = float(quiet_s)
        self._arm_window = timedelta(seconds=arm_window_s)
        self._arm_min_attackers = int(arm_min_attackers)
        self._presence_lost_settle_s = float(presence_lost_settle_s)
        self._clear_after_settled_s = float(clear_after_settled_s)
        self._fold_back = timedelta(seconds=fold_back_s)
        self._max_kill_age = max_kill_age
        # POD ISK — a genuine judgement call, flagged as one by the research
        # (open question 1). A pod's zkb.totalValue is its implants, which for
        # an FC can be billions and is a real loss; but loss_reconciler
        # deliberately drops capsule ISK so that "one loss = one number", and
        # this module stays CONSISTENT with it rather than inventing a second
        # convention. The pods are counted either way and surfaced in a note,
        # so nothing is silently lost — flip this to True if the owner decides
        # implants belong in the ISK column.
        self._count_pod_isk = bool(count_pod_isk)
        self._jspace_notice = bool(jspace_notice)
        # THE OFF SWITCH (`config["battle_ledger"]["enabled"]`, default True).
        # Distinct from `feed_enabled`, which says "zKill is off, so this is a
        # named blind spot" and still renders a panel saying so. `enabled=False`
        # means the OWNER does not want the feature at all: ingest is inert and
        # the view is permanently hidden, so nothing is drawn and nothing is
        # accumulated. `default_config.py` deliberately carries no battle_ledger
        # block (from_config owns the whole migration), so turning it off is
        # adding `"battle_ledger": {"enabled": false}` to config.json.
        self._enabled = bool(enabled)
        self._feed_enabled = bool(feed_enabled)
        self._utcnow = utcnow or (lambda: datetime.now(timezone.utc))
        self._monotonic = monotonic or time.monotonic

        # STARTUP REPLAY GUARD. ZKillMonitor._poll_loop rewinds `lookback = 200`
        # sequences (~30 minutes) on start so a restarted client catches up on
        # missed intel. Those kills are real and may be COUNTED once a fight is
        # armed (that is what recovers a mid-fight restart), but they must never
        # ARM one — otherwise every launch inside 30 minutes of a brawl opens a
        # battle panel for a fight that ended before FCTool did. Same cutoff
        # semantics as loss_reconciler._started_at.
        self._started_at = to_utc(started_at) or self._utcnow()

        self._own_ids: set[int] = set(own_ids or ())
        self._ally_ids: set[int] = set(ally_ids or ())
        self._identity_known = bool(identity_known)
        self._roster: set[int] = set()

        self._state = STATE_IDLE
        self._system_id: int | None = None
        self._totals: dict[str, _Totals] = {b: _Totals() for b in BUCKET_ORDER}
        self._confirmed_through: datetime | None = None
        self._fight_started_at: datetime | None = None
        self._last_arrival_mono: float | None = None
        self._settled_at_mono: float | None = None
        self._last_presence_ok_mono: float | None = None
        self._counted_ids: set[int] = set()
        self._candidates: list[_Candidate] = []
        self._candidate_ids: set[int] = set()
        self._fast: FastStrip | None = None
        self._revision = 0

    # ── configuration seams ────────────────────────────────────────────────

    @property
    def revision(self) -> int:
        """Bumped on every change a renderer could see. The wiring layer can
        compare it to skip a repaint (a busy fight delivers kills faster than
        the <= 1 Hz repaint budget)."""
        with self._lock:
            return self._revision

    @property
    def enabled(self) -> bool:
        """The owner's off switch. False => ingest is inert and the view stays
        hidden; the wiring layer also skips subscribing to the killmail feed, so
        a disabled ledger costs nothing at all."""
        return self._enabled

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    @property
    def system_id(self) -> int | None:
        with self._lock:
            return self._system_id

    @property
    def confirmed_through(self) -> datetime | None:
        with self._lock:
            return self._confirmed_through

    @property
    def started_at(self) -> datetime:
        with self._lock:
            return self._started_at

    def set_started_at(self, when: datetime | None = None):
        """Move the startup-replay cutoff. Used when the zKill monitor is
        (re)started later than the ledger — a fresh monitor replays a fresh
        200-sequence backlog, so the cutoff must move with it."""
        with self._lock:
            self._started_at = to_utc(when) or self._utcnow()

    def set_fleet_roster(self, char_ids: Iterable[int] | None):
        """Replace the live fleet roster (ESI /fleets/{id}/members/ character
        ids). Mirrors LossReconciler.set_fleet_roster deliberately — the wiring
        bite feeds both from one place."""
        with self._lock:
            self._roster = {cid for cid in (char_ids or ()) if cid}

    @property
    def fleet_roster(self) -> set[int]:
        with self._lock:
            return set(self._roster)

    def set_standings(self, own_ids: Iterable[int] | None = None,
                      ally_ids: Iterable[int] | None = None,
                      identity_known: bool | None = None):
        """Supply standings_cache's v2 split.

        `identity_known` is standings_cache.own_identity_known and is NOT
        cosmetic: while it is False, `ally_ids == friendly_ids`, so every
        alliance-mate misfiles as an Ally and every blue non-roster loss
        misfiles into Enemies. The view suppresses both of those rows rather
        than showing contaminated numbers (honesty rule 5).
        """
        with self._lock:
            if own_ids is not None:
                self._own_ids = set(own_ids)
            if ally_ids is not None:
                self._ally_ids = set(ally_ids)
            if identity_known is not None:
                self._identity_known = bool(identity_known)
            self._revision += 1

    def set_feed_enabled(self, enabled: bool):
        """config["zkillboard"]["enabled"] false => self.zkill_monitor is None
        in fc_gui, i.e. no killmails at all. The panel must SAY so, not render
        an empty table that reads as "no losses"."""
        with self._lock:
            if bool(enabled) != self._feed_enabled:
                self._feed_enabled = bool(enabled)
                self._revision += 1

    def set_fast_signal(self, text: str, latency_label: str):
        """Publish the separate fast lane (honesty rule 4).

        Whatever the wiring bite has that is genuinely fast — incoming damage
        on your own accounts (<1 s, native-preview only), your own accounts
        podded (10-30 s), the existing fleet-loss percentage (20-35 s) — goes
        HERE, never into a bucket row. It carries its own latency label because
        it has its own latency.
        """
        with self._lock:
            strip = FastStrip(text=text, latency_label=latency_label)
            if strip != self._fast:
                self._fast = strip
                self._revision += 1

    def clear_fast_signal(self):
        with self._lock:
            if self._fast is not None:
                self._fast = None
                self._revision += 1

    # ── state machine ──────────────────────────────────────────────────────

    def _transition(self, target: str):
        """The ONLY way `_state` changes. Illegal transitions raise rather than
        silently producing a state nobody designed for (e.g. a SETTLED ledger
        that never had a fight)."""
        current = self._state
        if target == current:
            return
        if target not in _LEGAL_TRANSITIONS.get(current, frozenset()):
            raise IllegalTransition(f"{current} -> {target}")
        self._state = target
        self._revision += 1

    def _clear(self):
        """Back to IDLE, ledger discarded. The replay cutoff is deliberately
        KEPT (it guards against the monitor's backlog, which the end of a fight
        has no bearing on) and so is the candidate buffer's pruning horizon."""
        if self._state != STATE_IDLE:
            self._transition(STATE_IDLE)
        self._system_id = None
        self._totals = {b: _Totals() for b in BUCKET_ORDER}
        self._confirmed_through = None
        self._fight_started_at = None
        self._last_arrival_mono = None
        self._settled_at_mono = None
        self._counted_ids.clear()
        self._revision += 1

    def dismiss(self):
        """Explicit user dismiss. Legal from FILLING and SETTLED alike."""
        with self._lock:
            self._clear()

    def reset(self):
        """Full reset, including the candidate buffer. For a fleet change or a
        monitor restart."""
        with self._lock:
            self._clear()
            self._candidates.clear()
            self._candidate_ids.clear()
            self._fast = None

    # ── presence (condition B) ─────────────────────────────────────────────

    def _presence_systems(self) -> set[int]:
        """System ids where one of the user's accounts currently is.

        The provider is the `own_presence` registry (bite 3), which has ALREADY
        applied PRESENCE_TTL_S. It is a local dict read, never a network call —
        the registry is fed opportunistically from location polls that already
        run, so this costs zero ESI.
        """
        if self._presence_provider is None:
            return set()
        try:
            return {int(sid) for sid in (self._presence_provider() or ()) if sid}
        except Exception:
            return set()

    # ── ingest ─────────────────────────────────────────────────────────────

    def ingest(self, kill_data: dict) -> "IngestResult":
        """Fold ONE killmail in. Safe to pass straight to
        `ZKillMonitor.on_killmail`; never raises on malformed input.

        Runs on the zKill poll thread. Touches no widget: the wiring layer
        marshals the RESULT (or just a repaint request) to Tk via `_post_ui`.
        """
        with self._lock:
            return self._ingest_locked(kill_data)

    def _ingest_locked(self, kill_data: dict) -> "IngestResult":
        if not self._enabled:
            return IngestResult(ACTION_IGNORED, REASON_DISABLED)
        if not self._feed_enabled:
            return IngestResult(ACTION_IGNORED, REASON_FEED_DISABLED)

        facts = parse_kill(kill_data)
        if facts is None:
            return IngestResult(ACTION_IGNORED, REASON_UNPARSEABLE)

        # Backfill gate. zKill accepts manually-posted historical killmails and
        # they enter the live stream like fresh ones (measured backfills of 6,
        # 29, 41 and 58 days). The SAME predicate ZKillMonitor._matches_filters
        # uses rejects them here, so the two cannot drift.
        nested = inner_killmail(kill_data) or {}
        if is_kill_stale(nested, self._max_kill_age, now=self._utcnow()):
            return IngestResult(ACTION_IGNORED, REASON_STALE, facts=facts)
        if facts.time is None:
            # Without a time there is no data clock, no replay guard and no arm
            # window. Dropping it is strictly better than inventing a fight.
            return IngestResult(ACTION_IGNORED, REASON_NO_TIME, facts=facts)
        if not facts.in_kspace:
            return IngestResult(ACTION_IGNORED, REASON_OUT_OF_KSPACE, facts=facts)

        # `armed_here` is computed BEFORE Condition A because the ally term is
        # gated on it, and it is the SAME expression the presence gate and the
        # counting branch below reuse — deliberately evaluated once so the
        # three cannot drift. It reads only `_system_id`/`_state`, neither of
        # which anything between here and its old site touches.
        armed_here = (self._system_id == facts.system_id
                      and self._state != STATE_IDLE)

        if not participates(facts, fleet_char_ids=self._roster,
                            own_ids=self._own_ids, ally_ids=self._ally_ids,
                            armed=armed_here):
            return IngestResult(ACTION_IGNORED, REASON_NOT_PARTICIPATING,
                                facts=facts)

        # Condition B — presence — gates ARMING, never COUNTING.
        #
        # `presence_provider` goes cold within ~15 s of warping out (the
        # location polls it reads are refreshed on that cadence), while the
        # ARRIVAL lag on this feed is ~2 minutes. Gating the COUNT on live
        # presence therefore drops precisely the last two minutes of the
        # engagement — the extraction, where a fleet takes its heaviest losses —
        # and the omission is asymmetric toward "we did fine", which is the one
        # failure mode this whole module exists to avoid. The `>=` floor
        # labelling would make it not-a-lie, but it would still be flattering.
        #
        # RESEARCH §3.4 is explicit: on presence loss, FREEZE — "do not clear —
        # the FC most wants to read the final ledger AFTER extracting".
        # PRESENCE_LOST_SETTLE_S still moves the LABEL to SETTLED from tick();
        # this gate is only about whether the DATA is allowed in.
        present = facts.system_id in self._presence_systems()
        if present and armed_here:
            self._last_presence_ok_mono = self._monotonic()
        elif not present and not armed_here:
            return IngestResult(ACTION_IGNORED, REASON_NO_PRESENCE, facts=facts)

        # Dedup. Only kills that passed BOTH qualification tests are recorded,
        # so neither set can grow with the K-space firehose (the same argument
        # loss_reconciler makes for its _seen_killmail_ids).
        kill_id = facts.killmail_id
        if kill_id is not None and (kill_id in self._counted_ids
                                    or kill_id in self._candidate_ids):
            return IngestResult(ACTION_IGNORED, REASON_DUPLICATE, facts=facts)

        replay = facts.time < self._started_at

        # A fight is FILLING elsewhere: exactly one ledger at a time, keyed on
        # system_id. Buffer this one so it can arm later if that fight ends.
        # (A SETTLED ledger deliberately does NOT block a new fight — research:
        # "CLEAR when a new fight arms elsewhere" — so this guard is on FILLING
        # alone and the settled ledger is discarded by _arm.)
        if self._state == STATE_FILLING and facts.system_id != self._system_id:
            self._buffer(facts, replay)
            return IngestResult(ACTION_BUFFERED, REASON_OTHER_SYSTEM, facts=facts)

        if armed_here:
            # Already armed for this system. Replayed kills inside the fight
            # window ARE counted — that is what recovers a mid-fight restart.
            # `armed_here` is the SAME value Condition A's ally term and the
            # presence gate above read, deliberately reused rather than
            # retyped: the three must not drift, or presence would gate a kill
            # this branch then counts (or an ally would qualify for a fight
            # this branch does not consider armed).
            if replay and (self._fight_started_at is None
                           or facts.time < self._fight_started_at - self._fold_back):
                self._buffer(facts, replay)
                return IngestResult(ACTION_BUFFERED, REASON_STARTUP_REPLAY,
                                    facts=facts)
            bucket = self._count(facts)
            return IngestResult(ACTION_COUNTED, REASON_OK, bucket=bucket,
                                facts=facts)

        # Nothing armed here. A replayed kill may never arm a fight.
        if replay:
            self._buffer(facts, replay)
            return IngestResult(ACTION_BUFFERED, REASON_STARTUP_REPLAY, facts=facts)

        if not self._arms(facts):
            self._buffer(facts, replay)
            return IngestResult(ACTION_BUFFERED, REASON_AWAITING_ARM, facts=facts)

        self._arm(facts)
        bucket = self._count(facts)
        self._fold_candidates(facts.system_id)
        return IngestResult(ACTION_COUNTED, REASON_OK, bucket=bucket,
                            facts=facts, armed=True)

    # ── arming ─────────────────────────────────────────────────────────────

    def _arms(self, facts: KillFacts) -> bool:
        """Is this killmail enough to open a battle panel?

        Either it is plainly a battle on its own (>= ARM_MIN_ATTACKERS on one
        mail), or a SECOND qualifying killmail for the same system sits within
        ARM_WINDOW_S of it on the DATA clock. A single gank in your system must
        not open a battle panel.
        """
        if facts.attacker_count >= self._arm_min_attackers:
            return True
        for candidate in self._candidates:
            other = candidate.facts
            if other.system_id != facts.system_id:
                continue
            if candidate.replay:
                # A replayed kill is not evidence that a fight is happening
                # NOW; it is history. It may be folded in once armed, never
                # counted toward arming.
                continue
            if abs(other.time - facts.time) <= self._arm_window:
                return True
        return False

    def _arm(self, facts: KillFacts):
        """Open the ledger on `facts`' system."""
        if self._state == STATE_SETTLED:
            # A different system re-arming: clear the old ledger first so the
            # machine never carries one fight's totals into another.
            if self._system_id != facts.system_id:
                self._clear()
        self._system_id = facts.system_id
        self._fight_started_at = facts.time
        self._settled_at_mono = None
        self._last_presence_ok_mono = self._monotonic()
        self._transition(STATE_FILLING)

    def _fold_candidates(self, system_id: int):
        """Retroactively count buffered kills for the newly-armed system.

        This is the half of the startup-replay rule that makes a mid-fight
        FCTool restart useful: replayed kills may not ARM a fight, but once one
        is armed by a genuinely fresh killmail, the ~30 minutes of replayed
        history for that same system inside the fight window belongs in the
        ledger. Bounded by FIGHT_FOLD_BACK_S so an unrelated brawl in the same
        system 25 minutes ago cannot be swept in.
        """
        if self._fight_started_at is None:
            return
        horizon = self._fight_started_at - self._fold_back
        keep: list[_Candidate] = []
        folded: list[KillFacts] = []
        for candidate in self._candidates:
            other = candidate.facts
            if other.system_id == system_id and other.time >= horizon:
                folded.append(other)
            else:
                keep.append(candidate)
        self._candidates = keep
        for other in sorted(folded, key=lambda f: f.time):
            if other.killmail_id is not None:
                self._candidate_ids.discard(other.killmail_id)
            self._count(other)

    def _buffer(self, facts: KillFacts, replay: bool):
        """Hold a qualifying kill outside the ledger, bounded.

        Pruned by age against the newest killmail_time seen, then hard-capped.
        EngagementTracker._kills next door grows without bound under
        watch_all=True (it never removes a system key); this must not repeat
        that pattern.
        """
        self._candidates.append(_Candidate(facts=facts, replay=replay))
        if facts.killmail_id is not None:
            self._candidate_ids.add(facts.killmail_id)
        horizon = facts.time - self._max_kill_age
        pruned = [c for c in self._candidates if c.facts.time >= horizon]
        if len(pruned) > REPLAY_BUFFER_MAX:
            pruned = pruned[-REPLAY_BUFFER_MAX:]
        if len(pruned) != len(self._candidates):
            self._candidates = pruned
            live = {c.facts.killmail_id for c in pruned
                    if c.facts.killmail_id is not None}
            self._candidate_ids &= live

    # ── accounting ─────────────────────────────────────────────────────────

    def _count(self, facts: KillFacts) -> str:
        """Fold one killmail into the bucket totals. Returns its bucket."""
        bucket = classify_victim(facts, fleet_char_ids=self._roster,
                                 own_ids=self._own_ids, ally_ids=self._ally_ids)
        totals = self._totals[bucket]
        if facts.is_structure_or_npc:
            # A dead Astrahus is not a "ship lost". Counted on its own line so
            # a sov fight's most important number is not silently dropped.
            totals.structures += 1
            totals.structure_isk += facts.isk_value
        elif facts.is_capsule:
            # SHIP+POD DEDUP: one dead pilot emits two killmails, so the pod
            # adds nothing to the ship count. Its ISK (implants) follows the
            # count_pod_isk decision — see __init__.
            totals.pods += 1
            totals.pod_isk += facts.isk_value
            if self._count_pod_isk:
                totals.isk += facts.isk_value
        else:
            totals.ships += 1
            totals.isk += facts.isk_value
            # Composition, ships only — deliberately inside the SAME branch
            # that increments `ships`, so the two can never disagree about
            # what a hull is (a capsule counted as a Capsule class would put
            # the pod back in the row it was just dedup'd out of).
            totals.ship_types[facts.victim_ship_type_id] += 1

        if facts.killmail_id is not None:
            self._counted_ids.add(facts.killmail_id)
        # The DATA clock: the newest killmail_time we hold for this fight.
        if self._confirmed_through is None or facts.time > self._confirmed_through:
            self._confirmed_through = facts.time
        if self._fight_started_at is None or facts.time < self._fight_started_at:
            self._fight_started_at = facts.time
        # The ARRIVAL clock: when the last kill REACHED us, which is what the
        # quiet window measures.
        self._last_arrival_mono = self._monotonic()
        if self._state == STATE_SETTLED:
            # The tail kept landing (or the fight resumed). Re-open rather than
            # drop data we already have — QUIET_S is a label, not a cutoff.
            self._transition(STATE_FILLING)
            self._settled_at_mono = None
        self._revision += 1
        return bucket

    # ── tick ───────────────────────────────────────────────────────────────

    def tick(self) -> bool:
        """Advance the timers. Returns True when the view changed.

        Call on the Tk thread at <= 1 Hz. Cheap: a couple of clock reads plus
        the presence registry lookup.
        """
        with self._lock:
            before = self._revision
            now_mono = self._monotonic()

            if self._state == STATE_FILLING:
                quiet_for = (now_mono - self._last_arrival_mono
                             if self._last_arrival_mono is not None else 0.0)
                presence = self._presence_systems()
                if self._system_id in presence:
                    self._last_presence_ok_mono = now_mono
                gone_for = (now_mono - self._last_presence_ok_mono
                            if self._last_presence_ok_mono is not None else 0.0)
                if quiet_for >= self._quiet_s or \
                        gone_for > self._presence_lost_settle_s:
                    self._transition(STATE_SETTLED)
                    self._settled_at_mono = now_mono
            elif self._state == STATE_SETTLED:
                if self._settled_at_mono is not None and \
                        (now_mono - self._settled_at_mono) > self._clear_after_settled_s:
                    self._clear()

            self._prune_candidates()
            return self._revision != before

    def _prune_candidates(self):
        """Age the candidate buffer off the DATA clock's NOW.

        `_buffer` prunes against the arriving kill's own time, which is right on
        the insert path; here the horizon has to be wall-relative or a buffer
        that stops receiving kills never drains (its newest member's timestamp
        would freeze the horizon with it).
        """
        if not self._candidates:
            return
        horizon = self._utcnow() - self._max_kill_age
        kept = [c for c in self._candidates if c.facts.time >= horizon]
        if len(kept) != len(self._candidates):
            self._candidates = kept
            self._candidate_ids = {c.facts.killmail_id for c in kept
                                   if c.facts.killmail_id is not None}

    # ── render model ───────────────────────────────────────────────────────

    def render_model(self, now: datetime | None = None) -> BattleLedgerView:
        """The pure description of what the panel should show.

        Every honesty rule is enforced HERE rather than in the renderer, so the
        Tk panel can be (and is) a thin, testable pass-through:

          * one shared `confirmed_through` and one `stamp_line` off the DATA
            clock, for all three buckets (rule 1);
          * `state_label` is FILLING/SETTLED — never a "LIVE" chip (rule 2),
            machine-checked by assert_no_live_language — and the floor is
            stated in words by the always-present NOTE_LAG note (see
            FLOOR_PREFIX for why the ">=" glyph itself is gone);
          * no event/flash field exists to animate (rule 3);
          * `fast` is a separate type in a separate field, and every row is
            asserted to share ONE provenance (rule 4);
          * an unattributable bucket carries None, not 0, plus a named note
            (rule 5).
        """
        with self._lock:
            if not self._enabled:
                # The owner's off switch: draw nothing, ever. Not even the
                # J-space blind-spot notice — naming a blind spot in a feature
                # that was switched off would be noise, not honesty.
                return self._hidden_view()
            reference = to_utc(now) or self._utcnow()
            notes: list[LedgerNote] = []

            if self._state == STATE_IDLE:
                # A hidden panel is not a misleading zero — it draws nothing at
                # all — so an idle ledger with a working feed simply stays down.
                # The ONE exception is J-space: a wormhole fight produces no
                # killmails, so "no fight detected" would be a lie by omission.
                # Rule 5 wants that blind spot named, not hidden.
                if (self._jspace_notice and self._feed_enabled
                        and self._presence_provider is not None
                        and any(sid >= JSPACE_MIN_SYSTEM_ID
                                for sid in self._presence_systems())):
                    return self._blind_view(NOTE_JSPACE, reference)
                return self._hidden_view()

            system_name = self._system_name(self._system_id)
            suppress_all = not self._feed_enabled
            if suppress_all:
                notes.append(LedgerNote(NOTE_FEED_DISABLED,
                                        _NOTE_TEXT[NOTE_FEED_DISABLED]))
            # Standings identity unknown => ally_ids == friendly_ids, so
            # alliance-mates misfile as Allies AND blue losses misfile into
            # Enemies (an ally's loss counted as an enemy loss reads as
            # "we're winning"). Both rows are suppressed; the roster join still
            # gives an honest, clearly-qualified Ours floor.
            standings_blind = not self._identity_known
            if standings_blind:
                notes.append(LedgerNote(
                    NOTE_STANDINGS_UNKNOWN,
                    _NOTE_TEXT[NOTE_STANDINGS_UNKNOWN] if self._roster
                    else _NOTE_TEXT[NOTE_NO_ATTRIBUTION]))

            rows: list[LedgerRow] = []
            for bucket in BUCKET_ORDER:
                totals = self._totals[bucket]
                suppressed = suppress_all
                qualifier = ""
                if standings_blind:
                    if bucket == BUCKET_OURS:
                        if self._roster:
                            qualifier = "roster only"
                        else:
                            suppressed = True
                    else:
                        suppressed = True
                rows.append(LedgerRow(
                    bucket=bucket,
                    label=BUCKET_LABELS[bucket],
                    ships=None if suppressed else totals.ships,
                    isk=None if suppressed else totals.isk,
                    pods=None if suppressed else totals.pods,
                    provenance=PROVENANCE_ZKILL,
                    qualifier=qualifier,
                    composition=() if suppressed else self._composition(totals),
                ))

            pods = sum(t.pods for t in self._totals.values())
            if pods and not self._count_pod_isk:
                notes.append(LedgerNote(
                    NOTE_PODS,
                    f"{pods} pod{'s' if pods != 1 else ''} not counted as ship "
                    f"losses (implant ISK excluded)."))
            structures = sum(t.structures for t in self._totals.values())
            if structures:
                notes.append(LedgerNote(
                    NOTE_STRUCTURES,
                    f"{structures} structure/NPC loss"
                    f"{'es' if structures != 1 else ''} not counted as ships."))
            notes.append(LedgerNote(
                NOTE_LAG,
                "Counts are floors — kills reach zKill ~2 min after they "
                "happen, so numbers only ever go up."))

            return self._build_view(
                visible=True,
                state=self._state,
                title=f"BATTLE LEDGER — {system_name}",
                system_id=self._system_id,
                system_name=system_name,
                rows=tuple(rows),
                notes=tuple(notes),
                reference=reference,
            )

    # ── view builders (lock already held) ──────────────────────────────────

    def _hidden_view(self) -> BattleLedgerView:
        return BattleLedgerView(
            visible=False, state=STATE_IDLE, state_label=STATE_LABELS[STATE_IDLE],
            title="", system_id=None, system_name="", confirmed_through=None,
            stamp_line="", lag_line="", floor_prefix=FLOOR_PREFIX, rows=(),
            notes=(), fast=self._fast, killed_note="",
            revision=self._revision, battle_report_url="",
        )

    def _blind_view(self, kind: str, reference: datetime) -> BattleLedgerView:
        """Visible, but with NO numbers at all — every bucket None (rule 5)."""
        rows = tuple(
            LedgerRow(bucket=b, label=BUCKET_LABELS[b], ships=None, isk=None,
                      pods=None, provenance=PROVENANCE_ZKILL)
            for b in BUCKET_ORDER
        )
        return self._build_view(
            visible=True, state=STATE_IDLE, title="BATTLE LEDGER",
            system_id=None, system_name="", rows=rows,
            notes=(LedgerNote(kind, _NOTE_TEXT[kind]),), reference=reference,
        )

    def _build_view(self, *, visible: bool, state: str, title: str,
                    system_id: int | None, system_name: str,
                    rows: tuple, notes: tuple,
                    reference: datetime) -> BattleLedgerView:
        # RULE 4, MECHANICALLY: one provenance across every row. A future edit
        # that stitches a fast ESI-derived Ours row into this table fails here
        # instead of shipping a false comparison to an FC.
        provenances = {row.provenance for row in rows}
        if len(provenances) > 1:
            raise AssertionError(
                f"battle ledger rows carry mixed provenance {sorted(provenances)} "
                "— all three buckets must be uniformly zKill-sourced under ONE "
                "timestamp; fast signals belong in BattleLedgerView.fast")
        stamp_line = format_stamp_line(self._confirmed_through, reference)
        state_label = STATE_LABELS[state]
        lag_line = ("kills still arriving" if state == STATE_FILLING
                    else "no new kills for " + format_behind(self._quiet_s))
        killed_note = "Enemies row = what your side killed."
        # EVERY lagged string the view carries, not a sample of them: a bucket
        # label or a row qualifier renders just as prominently as the title, and
        # `BUCKET_LABELS[BUCKET_OURS] = "Ours (live)"` used to sail straight
        # through. `fast` is the ONE exemption and is stated as such below.
        assert_no_live_language(title, "title")
        assert_no_live_language(stamp_line, "stamp_line")
        assert_no_live_language(state_label, "state_label")
        assert_no_live_language(lag_line, "lag_line")
        assert_no_live_language(killed_note, "killed_note")
        for row in rows:
            assert_no_live_language(row.label, f"row:{row.bucket}:label")
            assert_no_live_language(row.qualifier, f"row:{row.bucket}:qualifier")
        for note in notes:
            assert_no_live_language(note.text, f"note:{note.kind}")
        # DELIBERATELY UNCHECKED: `self._fast`. The fast lane is the honest
        # exception — it really is sub-second and it carries its own latency
        # label — so freshness language there is true, not a lie.
        return BattleLedgerView(
            visible=visible,
            state=state,
            state_label=state_label,
            title=title,
            system_id=system_id,
            system_name=system_name,
            confirmed_through=self._confirmed_through,
            stamp_line=stamp_line,
            lag_line=lag_line,
            floor_prefix=FLOOR_PREFIX,
            rows=rows,
            notes=notes,
            fast=self._fast,
            killed_note=killed_note,
            revision=self._revision,
            # The fight's OWN start, off the passed system id — `_blind_view`
            # passes None there while `self._system_id` may still hold a value,
            # and a blind view must not offer a report for a fight it is
            # refusing to state numbers for.
            battle_report_url=warbeacon_url(system_id, self._fight_started_at),
        )

    def _composition(self, totals: _Totals) -> tuple:
        """This bucket's hulls as resolved `CompositionEntry` items.

        CALLED WITH `self._lock` HELD, on the Tk thread, at 1 Hz, once per
        DISTINCT type id — so `resolve_ship_group` is bound by the same
        non-blocking contract as `resolve_system_name` (see the class
        docstring). Everything it cannot name collapses into ONE unnamed entry
        rather than N mystery rows: the total still reconciles with the bucket's
        ship count, which is the property that makes the tooltip trustworthy.
        """
        named: Counter = Counter()
        unnamed = 0
        for type_id, count in totals.ship_types.items():
            name = self._ship_group_name(type_id)
            if name:
                named[name] += count
            else:
                unnamed += count
        entries = [CompositionEntry(name, count)
                   for name, count in named.items()]
        if unnamed:
            entries.append(CompositionEntry("", unnamed))
        return tuple(sorted(entries, key=_composition_sort_key))

    def _ship_group_name(self, type_id: int) -> str:
        """A hull's ship-group name, or "" when it cannot be resolved offline.

        Never raises: a broken/absent resolver degrades to the unnamed bucket,
        which renders as a plain "N ships". That is the honest failure — the
        ship count itself is unaffected.
        """
        if not type_id or self._resolve_ship_group is None:
            return ""
        try:
            resolved = self._resolve_ship_group(type_id)
        except Exception:
            return ""
        if isinstance(resolved, str) and resolved.strip():
            return resolved.strip()
        return ""

    def _system_name(self, system_id: int | None) -> str:
        """Resolve the fight's system name. CALLED WITH `self._lock` HELD, on the
        Tk thread, at 1 Hz — so the injected resolver must be offline/pure (see
        the class docstring's non-blocking contract)."""
        if not system_id:
            return "?"
        if self._resolve_system_name is None:
            return str(system_id)
        try:
            resolved = self._resolve_system_name(system_id)
        except Exception:
            return str(system_id)
        return resolved if isinstance(resolved, str) and resolved else str(system_id)


@dataclass(frozen=True)
class IngestResult:
    """What `BattleLedger.ingest` did with one killmail."""
    action: str
    reason: str = REASON_OK
    bucket: str | None = None
    facts: KillFacts | None = None
    armed: bool = False

    @property
    def counted(self) -> bool:
        return self.action == ACTION_COUNTED


# ── Config adapter (app config -> engine) ───────────────────────────────────
# FCTool never deep-merges config.json against DEFAULT_CONFIG, so an install
# that predates this feature simply HAS NO `battle_ledger` block and a
# hand-edited one may hold anything. Reading the block defensively IS the whole
# migration — same shape as loss_reconciler.from_config, deliberately.
#
#   "battle_ledger": {
#       "enabled": true,            # THE OFF SWITCH (see BattleLedger.enabled)
#       "quiet_s": 360,             # SETTLED after this long with no ARRIVAL
#       "arm_window_s": 120,        # two kills this close arm a fight
#       "arm_min_attackers": 10,    # ...or one killmail this big
#       "count_pod_isk": false,     # implants in the ISK column?
#       "jspace_notice": true       # name the wormhole blind spot
#   }

CONFIG_KEY = "battle_ledger"


def config_block(config) -> dict:
    block = config.get(CONFIG_KEY) if isinstance(config, dict) else None
    return block if isinstance(block, dict) else {}


def _config_number(value, fallback: float) -> float:
    """Coerce a config number; anything non-numeric or negative falls back
    rather than raising (a hand-edited config must never break the ledger)."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    return number if number >= 0 else fallback


def _config_bool(value, fallback: bool) -> bool:
    return bool(value) if isinstance(value, bool) else fallback


def from_config(config, **overrides) -> BattleLedger:
    """Build a `BattleLedger` from the app config. `overrides` win, so the
    wiring bite can still inject the providers and clocks."""
    block = config_block(config)
    kwargs = dict(
        enabled=_config_bool(block.get("enabled"), True),
        quiet_s=_config_number(block.get("quiet_s"), QUIET_S),
        arm_window_s=_config_number(block.get("arm_window_s"), ARM_WINDOW_S),
        arm_min_attackers=int(_config_number(block.get("arm_min_attackers"),
                                             ARM_MIN_ATTACKERS)),
        count_pod_isk=_config_bool(block.get("count_pod_isk"), False),
        jspace_notice=_config_bool(block.get("jspace_notice"), True),
    )
    kwargs.update(overrides)
    return BattleLedger(**kwargs)
