"""
Fleet loss reconciliation — the zKillboard side of loss tracking.

`loss_tracker.FleetLossTracker` INFERS deaths from ship->capsule transitions in
the ESI fleet poll. It is fast (~20-35s) but structurally blind: a podded pilot
respawns DOCKED in a clone bay, which the confirm branch can never fire on, and
that class alone is 26-43% of PvP ship losses. zKillboard is the inverse —
ground truth, but a median ~2 minutes and a p90 ~4.5 minutes behind the event.

This module is the reconciler that makes the two work together. It rides the
killmail feed the intel tab ALREADY runs (`ZKillMonitor.on_killmail`), so its
marginal network cost is exactly zero: it makes no requests of its own and, at
its defaults, resolves no names.

What it does with a fleet-member killmail:
  * matches an already-inferred ESI death  -> CONFIRM it (attach the killmail
    id, the exact victim hull, and the real ISK value);
  * matches nothing                        -> ADD a death the capsule logic
    structurally could not see;
  * is the pilot's CAPSULE killmail        -> never counted (one dead pilot
    emits TWO killmails); it only marks the ship loss as `podded`.

Deliberately NOT here:
  * Thresholds/alerts. The reconciler only ever ADDS to a ledger; it never
    fires, un-fires, or re-orders the tracker's fire-once thresholds. Whether a
    late-arriving killmail should retro-fire a "50% lost" alert is an open
    owner decision, so the wiring layer owns it.
  * The Allies bucket. Goal 3's "ours" test is a direct join of
    `victim.character_id` against the live ESI fleet roster and needs no
    standings split at all. `classify_victim` already takes the own/ally id
    sets Goal 4 will supply, so adding that bucket later reshapes nothing.
  * ISK from capsule killmails. `DeathEvent.isk_value` is the SHIP killmail's
    `zkb.totalValue` only; implant value on the pod killmail is dropped.
    Revisitable, but it keeps one loss = one number.

Purity: no Tk, no fc_gui import, no network. `zkill_monitor` is imported for
ONE thing — the shared `is_kill_stale` predicate (importing it performs no
I/O). Every ESI-touching capability (name resolution, tackle classification) is
an INJECTED callable defaulting to off, so the engine's default cost is zero.

Thread safety: `ZKillMonitor` calls the hook on its poll thread while the fleet
poll feeds `note_esi_deaths` from another, so all public mutators take an
internal lock. Callers may therefore ingest directly on the zKill thread and
marshal only the RESULT to Tk (`_post_ui`), or marshal the whole call — both
are correct. Nothing in here touches a widget.

That lock is NEVER held across an injected callable. Both of them can reach the
network, and the Tk thread takes the same lock to read the ledger for the status
label, so `ingest` builds the record under the lock, drops it, resolves, and
re-acquires only to write the names back and PUBLISH (`_resolve_deferred`).

PUBLICATION BARRIER — the second half of that deal. A record whose names are
still being resolved is staged in `_zkill_pending`: the engine's own matchers
see it (so a pod, or an inference arriving in that window, still finds the loss
it belongs to) but no public reader does. The wiring logs each zKill-only loss
EXACTLY ONCE off a monotonic watermark, so a Tk-thread reader that caught a
record mid-resolve would bake its `str(id)` placeholder into the loss log
permanently. A record therefore becomes visible only once its names are final.
"""

import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterable

from loss_tracker import (
    CAPSULE_TYPE_IDS,
    DEATH_SOURCE_ESI,
    DEATH_SOURCE_ZKILL,
    DeathEvent,
)
from zkill_monitor import MAX_KILL_AGE, is_kill_stale

# ── Source selector modes (config: loss_tracking.source) ────────────────────
# Aliased from loss_tracker so the death-provenance strings and the selector
# mode names can never drift apart — they are deliberately the same vocabulary.
SOURCE_ESI = DEATH_SOURCE_ESI        # "esi"   — capsule transitions only
SOURCE_ZKILL = DEATH_SOURCE_ZKILL    # "zkill" — killmails only (non-boss case)
SOURCE_HYBRID = "hybrid"             # ESI drives, zKill reconciles
SOURCE_MODES = (SOURCE_ESI, SOURCE_ZKILL, SOURCE_HYBRID)
DEFAULT_SOURCE = SOURCE_HYBRID


def normalize_source(value) -> str:
    """Coerce a config value to a known mode. Anything unrecognised (None, a
    typo, a non-string) falls back to DEFAULT_SOURCE rather than raising — a
    hand-edited config must never break loss tracking."""
    if isinstance(value, str):
        candidate = value.strip().lower()
        if candidate in SOURCE_MODES:
            return candidate
    return DEFAULT_SOURCE


# ── Ownership buckets ───────────────────────────────────────────────────────
# Precedence (research §6.3): fleet roster -> own corp/alliance -> allies ->
# enemies. Goal 3 supplies only the roster, which collapses this to a two-way
# ours/not-ours split; Goal 4 adds the standings sets without a signature change.
BUCKET_OURS = "ours"
BUCKET_ALLIES = "allies"
BUCKET_ENEMIES = "enemies"

# ── Match window (research §4.2) ────────────────────────────────────────────
# zKill's killmail_time is the TRUE death instant; the ESI inference fires
# 15-45s AFTER the event (two 15s polls plus the pod transition), so the window
# is asymmetric: an inferred death may precede the killmail by MATCH_LEAD_S
# (clock skew / an early poll) and may trail it by MATCH_WINDOW_S.
#   killmail_time - MATCH_LEAD_S  <=  inferred_timestamp  <=  killmail_time + MATCH_WINDOW_S
# This comparison is ONLY possible because DeathEvent.timestamp is aware UTC;
# it was naive local time until the 2026-07-25 fix and raised TypeError here.
MATCH_LEAD_S = 30
MATCH_WINDOW_S = 300

# ── Pod match window (a DIFFERENT relation, deliberately not the one above) ──
# The window above is anchored on a SHIP killmail and asks "did the ESI poll
# notice this death?". The pod relation asks "which ship loss does this capsule
# belong to?" — and physics fixes that order: the ship always dies BEFORE its
# pilot's capsule. So the ship loss may LEAD the pod generously (a pilot can sit
# or run in a pod for minutes before someone finishes the job) and may TRAIL it
# only by the ESI inference lag (a matching ESI-inferred death is stamped 15-45s
# after the true ship loss, so it can carry a stamp just after a fast pod).
#   pod_time - POD_MATCH_LEAD_S <= ship_loss_stamp <= pod_time + POD_MATCH_TRAIL_S
# Reusing MATCH_LEAD_S/MATCH_WINDOW_S here — as this module did until the
# 2026-07-25 fix — applies the window BACKWARDS: it accepts a ship loss 300s
# AFTER the pod (impossible) and rejects one 31s before it (the ordinary case).
# `podded` is precisely the signal that the ESI capsule detector was blind for
# that pilot, so under-reporting it defeats the flag's purpose.
POD_MATCH_LEAD_S = 300
POD_MATCH_TRAIL_S = 60

# ── Result vocabulary ───────────────────────────────────────────────────────
ACTION_CONFIRMED = "confirmed"   # matched an ESI-inferred death, enriched it
ACTION_ADDED = "added"           # zKill-only loss; ESI was blind to it
ACTION_PODDED = "podded"         # capsule killmail; flagged a loss, added none
ACTION_IGNORED = "ignored"       # see `reason`

REASON_OK = ""
REASON_SOURCE_DISABLED = "source_disabled"   # mode is "esi"; zKill is inert
REASON_UNPARSEABLE = "unparseable"
REASON_NO_VICTIM = "no_victim_character"     # structure/NPC loss
REASON_NO_TIME = "no_killmail_time"
REASON_STALE = "stale"                       # older than MAX_KILL_AGE
REASON_STARTUP_REPLAY = "startup_replay"     # predates this reconciler
REASON_DUPLICATE = "duplicate_killmail"
REASON_NOT_OURS = "not_ours"
REASON_POD_MARKED = "pod_marked_existing_loss"
REASON_POD_DEFERRED = "pod_awaiting_ship_loss"


@dataclass(frozen=True)
class Killmail:
    """The handful of fields loss reconciliation needs from one killmail.

    `time` is AWARE UTC (or None when the killmail carried no parseable
    `killmail_time`). `isk_value` is `zkb.totalValue` in raw ISK.
    """
    killmail_id: int | None
    character_id: int
    corporation_id: int
    alliance_id: int
    ship_type_id: int
    system_id: int
    time: datetime | None
    isk_value: float

    @property
    def is_capsule(self) -> bool:
        return self.ship_type_id in CAPSULE_TYPE_IDS


@dataclass(frozen=True)
class ReconcileResult:
    """What `LossReconciler.ingest` did with one killmail.

    `death` is the DeathEvent that was confirmed/added/flagged, or None when
    nothing was touched. `killmail` is None only when the input could not be
    parsed at all.
    """
    action: str
    reason: str = REASON_OK
    death: DeathEvent | None = None
    killmail: Killmail | None = None

    @property
    def counted(self) -> bool:
        """True when this killmail changed the reconciled loss COUNT (i.e. it
        added a death). A confirmation enriches an existing death; a pod flags
        one. Neither moves the count — that is the ship+pod dedup rule."""
        return self.action == ACTION_ADDED


# ── datetime normalisation ──────────────────────────────────────────────────
# The project carries MIXED aware/naive timestamps, so every value entering the
# match arithmetic is funnelled through one of these two helpers first.

def to_utc(value: datetime | None) -> datetime | None:
    """Normalise an IN-PROCESS datetime to aware UTC.

    A naive value is read as LOCAL time — that is what `datetime.now()`
    produces, and `astimezone()` on a naive datetime assumes local. Contrast
    `parse_killmail_time`, which reads a naive *string* as UTC.
    """
    if value is None:
        return None
    return value.astimezone(timezone.utc)


def parse_killmail_time(value) -> datetime | None:
    """Parse a killmail's `killmail_time` to aware UTC, or None.

    Accepts the ESI string form ("2026-07-25T08:36:03Z") or an already-parsed
    datetime. A string carrying no UTC offset is read as UTC — killmail times
    are UTC by ESI spec, so assuming local here would silently shift every
    match window by the user's timezone.
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


_EPOCH = datetime.min.replace(tzinfo=timezone.utc)


def _sort_key(death: DeathEvent) -> datetime:
    """Chronological sort key that survives a MIXED aware/naive ledger — every
    stamp is normalised before comparison, so one hand-built naive DeathEvent
    can never make `sorted()` raise."""
    return to_utc(death.timestamp) or _EPOCH


def _inner_killmail(kill_data: dict) -> dict | None:
    """Unwrap whichever killmail envelope we were handed.

    Handles the internal normalized shape ({"killmail": ..., "zkb": ...}), the
    raw R2Z2 shape ({"esi": ..., "zkb": ...}) and a bare killmail dict —
    mirroring `ZKillMonitor._normalize_kill`, which the production hook has
    already applied.
    """
    if not isinstance(kill_data, dict):
        return None
    for key in ("killmail", "esi"):
        inner = kill_data.get(key)
        if isinstance(inner, dict):
            return inner
    return kill_data


def _as_int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def parse_killmail(kill_data: dict) -> Killmail | None:
    """Extract a `Killmail` from any killmail envelope, or None if it is not a
    dict at all. A killmail with no victim character (a structure or NPC loss)
    still parses — it comes back with `character_id == 0` so the caller can
    report *why* it was skipped."""
    inner = _inner_killmail(kill_data)
    if inner is None:
        return None
    victim = inner.get("victim") or {}
    if not isinstance(victim, dict):
        victim = {}
    zkb = kill_data.get("zkb") if isinstance(kill_data, dict) else None
    if not isinstance(zkb, dict):
        zkb = {}
    killmail_id = inner.get("killmail_id", kill_data.get("killmail_id"))
    try:
        isk_value = float(zkb.get("totalValue") or 0.0)
    except (TypeError, ValueError):
        isk_value = 0.0
    return Killmail(
        killmail_id=_as_int(killmail_id) or None,
        character_id=_as_int(victim.get("character_id")),
        corporation_id=_as_int(victim.get("corporation_id")),
        alliance_id=_as_int(victim.get("alliance_id")),
        ship_type_id=_as_int(victim.get("ship_type_id")),
        system_id=_as_int(inner.get("solar_system_id")),
        time=parse_killmail_time(inner.get("killmail_time")),
        isk_value=isk_value,
    )


def classify_victim(km: Killmail, *, fleet_char_ids=frozenset(),
                    own_ids=frozenset(), ally_ids=frozenset()) -> str:
    """Bucket a killmail's victim as "ours" / "allies" / "enemies".

    Precedence is fixed: fleet roster -> own corp/alliance ids -> ally ids ->
    enemies. `own_ids`/`ally_ids` hold CORPORATION and ALLIANCE ids (a flat set
    of each, as `standings_cache` v2 will expose them); `fleet_char_ids` holds
    CHARACTER ids.

    With own/ally empty — Goal 3's configuration — this degrades to an honest
    two-way split: on the roster is "ours", everything else is "enemies". Goal 4
    passes the standings sets to get the real three-way answer.
    """
    if km.character_id and km.character_id in fleet_char_ids:
        return BUCKET_OURS
    for entity_id in (km.corporation_id, km.alliance_id):
        if entity_id and entity_id in own_ids:
            return BUCKET_OURS
    for entity_id in (km.corporation_id, km.alliance_id):
        if entity_id and entity_id in ally_ids:
            return BUCKET_ALLIES
    return BUCKET_ENEMIES


@dataclass
class _Deferred:
    """Injected-callable work `ingest` performs AFTER it drops the lock.

    Both optional capabilities can reach the network — `zkill_monitor.
    resolve_name` does a rate-limited `requests.get` on a cache miss, and
    `ship_classes.is_tackle` can trigger a type->group lookup — while the Tk
    thread takes this same lock to read the ledger for the status label. So the
    record is created with placeholder names inside the lock, the callables run
    outside it, and only the short write-back re-acquires.

    `lookups` are (attribute, entity_id, category) triples for `_name`.
    `placeholder_only` writes a resolved name only while the attribute still
    holds its `str(entity_id)` placeholder (another thread may have filled in a
    real one meanwhile); `_confirm`'s hull rename deliberately clears it, since
    replacing the ESI guess IS the point. `expect_ship` guards that rename
    against a concurrent re-confirm onto a different hull. `tackle_type_id` of 0
    skips the tackle classification.

    The write-back also PUBLISHES `death` if it is staged in `_zkill_pending`
    (the ADD path). The CONFIRM path stages nothing — it enriches a death that
    is already in `_esi_deaths` and already carries a real ESI-supplied
    `ship_name`, so the worst a reader can catch there is the previous hull
    name, never a `str(id)` placeholder.
    """
    death: DeathEvent
    lookups: tuple = ()
    placeholder_only: bool = True
    expect_ship: int = 0
    tackle_type_id: int = 0


class LossReconciler:
    """Reconciles zKillboard killmails against ESI-inferred fleet deaths.

    Typical wiring (the next bite):

        rec = LossReconciler(source=normalize_source(cfg.get("source")))
        zkill_monitor.on_killmail = rec.ingest          # poll thread
        ...
        rec.set_fleet_id(fleet_id)                      # each fleet poll
        rec.set_fleet_roster(m["character_id"] for m in members)
        rec.note_esi_deaths(new_deaths)                 # from tracker.update()

    Optional injected capabilities (all default to OFF so the engine costs
    nothing on the wire):
      * `is_tackle(ship_type_id) -> bool` — classify an ADDED death's hull.
        `ship_classes.is_tackle` fits, but note it can trigger an ESI
        type->group lookup for an uncached hull.
      * `resolve_name(entity_id, category) -> str` — name lookup with
        `zkill_monitor.resolve_name`'s exact signature. Without it, names on
        ADDED deaths fall back to `str(id)` (the same fallback that function
        uses on failure).
    """

    def __init__(self, *, source: str = DEFAULT_SOURCE,
                 match_window_s: int = MATCH_WINDOW_S,
                 match_lead_s: int = MATCH_LEAD_S,
                 started_at: datetime | None = None,
                 max_kill_age: timedelta = MAX_KILL_AGE,
                 is_tackle: Callable[[int], bool] | None = None,
                 resolve_name: Callable[[int, str], str] | None = None,
                 own_ids: Iterable[int] | None = None,
                 ally_ids: Iterable[int] | None = None):
        self._lock = threading.RLock()
        self._source = normalize_source(source)
        self._match_window = timedelta(seconds=match_window_s)
        self._match_lead = timedelta(seconds=match_lead_s)
        # The pod relation is NOT configurable: it is a physical ordering, not a
        # tuning knob like the poll-lag window above.
        self._pod_lead = timedelta(seconds=POD_MATCH_LEAD_S)
        self._pod_trail = timedelta(seconds=POD_MATCH_TRAIL_S)
        self._max_kill_age = max_kill_age
        self._is_tackle = is_tackle
        self._resolve_name = resolve_name
        # STARTUP REPLAY GUARD. ZKillMonitor._poll_loop deliberately rewinds
        # `lookback = 200` sequences (~30 minutes) on start so a restarted
        # client catches up on the intel it missed. A freshly-built reconciler
        # would otherwise ingest that whole backlog as live fleet losses —
        # against a baseline that also restarted from zero, which is exactly
        # how a false "50% lost" alert gets manufactured. Anything that died
        # before this reconciler existed is not this session's loss.
        self._started_at = to_utc(started_at) or datetime.now(timezone.utc)
        self._own_ids: set[int] = set(own_ids or ())
        self._ally_ids: set[int] = set(ally_ids or ())
        self._roster: set[int] = set()
        self._fleet_id: int | None = None
        self._esi_deaths: list[DeathEvent] = []
        self._zkill_deaths: list[DeathEvent] = []
        # PUBLICATION STAGING (see the module docstring). zKill records created
        # with `str(id)` placeholders live here until `_resolve_deferred` has
        # filled their real names in; only then are they appended to
        # `_zkill_deaths`, where public readers can see them. The engine's own
        # matchers (`_match_zkill_twin`, `_all_records`) read BOTH lists, so a
        # pod or an inference arriving mid-resolve still finds its loss — the
        # barrier hides an unfinished record from readers, never from the
        # engine. With nothing injected there is nothing to resolve and a
        # record is published immediately, so this stays empty.
        self._zkill_pending: list[DeathEvent] = []
        # ESI-inferred deaths whose own killmail BEAT them here. The zKill
        # record is the survivor (it holds the exact hull and the real ISK) and
        # this is the inference's shadow: kept OUT of `_esi_deaths` so the
        # hybrid ledger cannot count the loss twice, but still part of the "esi"
        # ledger, because switching source must never make a death the capsule
        # detector really did infer disappear.
        self._absorbed_esi_deaths: list[DeathEvent] = []
        # id()s of zKill records that already swallowed an inference. A record
        # absorbs at most ONE: a pilot who re-ships and dies again inside the
        # window is two losses, not one. (The ids stay valid because the objects
        # live in `_zkill_deaths` until `reset` clears both together.)
        self._absorbed_zkill: set[int] = set()
        # Killmail ids already ingested. Only OUR losses are recorded, so this
        # cannot grow with the K-space firehose.
        self._seen_killmail_ids: set[int] = set()
        # Capsule killmails still waiting for the ship loss they belong to, as
        # (character_id, pod_time). Killmail order is not guaranteed, so a pod
        # can reach us before its own ship loss does. Entries are CONSUMED by
        # the loss they flag: a pilot podded once is not podded forever.
        self._pending_pods: list[tuple[int, datetime]] = []

    # ── configuration ──────────────────────────────────────────────────────

    @property
    def source(self) -> str:
        return self._source

    def set_source(self, source: str):
        """Switch mode live (the config radio persists immediately).

        Ledgers are kept, and NO mode doubles a death already recorded. That
        holds because composition, not ingestion, applies the mode: dedup,
        absorption, pod flagging and confirmation are all mode-free, and
        `_ledger()` is the only place `self._source` is read on the way out. It
        was not always so — gating the twin-absorb on "hybrid" meant a
        zkill -> hybrid switch counted every loss twice (the inference AND its
        killmail), and a fire-once threshold cannot be walked back once a
        phantom 50% has fired.

        It is NOT lossless in every direction, though, and one branch on the
        mode does exist on the ingest path: in "esi" mode `_ingest_locked`
        stops at its first line and records NOTHING at all. So `esi -> zkill`
        reports 0 — there is no killmail-backed record to compose. That is the
        intended behaviour rather than a hole: in "esi" mode the wiring also
        skips `note_esi_deaths`, so the engine starts empty either way. Note
        the shape of that branch — a TOTAL stop, never a partial one. A branch
        that recorded some things and not others would reintroduce the
        doubling class above.
        """
        with self._lock:
            self._source = normalize_source(source)

    @property
    def started_at(self) -> datetime:
        return self._started_at

    def set_started_at(self, when: datetime | None = None):
        """Move the startup-replay cutoff. Used when the zKill monitor is
        (re)started later than the reconciler — a fresh monitor replays a fresh
        200-sequence backlog, so the cutoff must move with it."""
        with self._lock:
            self._started_at = to_utc(when) or datetime.now(timezone.utc)

    def set_standings(self, own_ids: Iterable[int] | None = None,
                      ally_ids: Iterable[int] | None = None):
        """Goal 4 seam: supply own/ally corp+alliance ids for `classify_victim`.
        Unused by Goal 3, whose "ours" test is the roster join alone."""
        with self._lock:
            if own_ids is not None:
                self._own_ids = set(own_ids)
            if ally_ids is not None:
                self._ally_ids = set(ally_ids)

    def set_fleet_roster(self, char_ids: Iterable[int] | None):
        """Replace the live fleet roster (ESI `/fleets/{id}/members/` character
        ids). An empty roster makes every killmail "not ours" — which is also
        the correct answer before the first successful fleet poll."""
        with self._lock:
            self._roster = {cid for cid in (char_ids or ()) if cid}

    @property
    def fleet_roster(self) -> set[int]:
        with self._lock:
            return set(self._roster)

    def set_fleet_id(self, fleet_id: int | None):
        """Mirror of `FleetLossTracker.set_fleet_id` — reset on fleet change so
        the two ledgers stay in step."""
        with self._lock:
            if fleet_id != self._fleet_id:
                self.reset(fleet_id)

    def reset(self, new_fleet_id: int | None = None):
        """Clear every ledger for a new fleet. The startup-replay cutoff is
        deliberately KEPT: it guards against the monitor's backlog, which a
        fleet change has no bearing on."""
        with self._lock:
            self._fleet_id = new_fleet_id
            self._roster.clear()
            self._esi_deaths.clear()
            self._zkill_deaths.clear()
            # A record still resolving belongs to the fleet being left, so
            # dropping it here is correct: `_publish_locked` will not find it
            # and the write-back becomes a no-op on an unreferenced object.
            self._zkill_pending.clear()
            self._absorbed_esi_deaths.clear()
            self._absorbed_zkill.clear()
            self._seen_killmail_ids.clear()
            self._pending_pods.clear()

    # ── ESI side ───────────────────────────────────────────────────────────

    def note_esi_deaths(self, deaths: Iterable[DeathEvent] | None):
        """Record the deaths `FleetLossTracker.update()` just inferred.

        These are the SAME objects the tracker holds, not copies. Confirmation
        therefore enriches the tracker's own DeathEvents in place (exact hull,
        killmail id, ISK) — deliberate, and the reason nothing here ever
        touches `_was_tackle`: that attribute feeds the tracker's
        `relevant_deaths_count`, and silently reclassifying a counted death
        would move a threshold ratio the reconciler has no business moving.

        Deaths already covered by a zKill-sourced record are NOT added again.
        zKill usually trails ESI by ~2 minutes, but its measured minimum ingest
        lag is 5-14s against ESI's 20-35s two-poll confirm, so the killmail CAN
        land first — and counting both would count the same loss twice.

        That dedup is deliberately MODE-INDEPENDENT. It used to run only in
        hybrid, which meant a `zkill` -> `hybrid` switch doubled every loss
        recorded before it; see `set_source`.
        """
        with self._lock:
            for death in deaths or ():
                if death is None:
                    continue
                if self._take_pending_pod(death.character_id, death.timestamp):
                    death.podded = True
                twin = self._match_zkill_twin(death)
                if twin is not None:
                    self._absorb_into_zkill_record(twin, death)
                    self._absorbed_zkill.add(id(twin))
                    self._absorbed_esi_deaths.append(death)
                    continue
                self._esi_deaths.append(death)

    # ── zKill side ─────────────────────────────────────────────────────────

    def bucket_of(self, kill_data: dict) -> str | None:
        """Ownership bucket for a raw killmail, or None if it cannot be parsed."""
        km = parse_killmail(kill_data)
        if km is None:
            return None
        with self._lock:
            return classify_victim(
                km, fleet_char_ids=self._roster,
                own_ids=self._own_ids, ally_ids=self._ally_ids,
            )

    def ingest(self, kill_data: dict) -> ReconcileResult:
        """Reconcile ONE killmail. Safe to pass straight to
        `ZKillMonitor.on_killmail`; never raises on malformed input.

        The lock is DROPPED before any injected callable runs. `resolve_name` is
        meant to be wired to `zkill_monitor.resolve_name`, which does a
        rate-limited `requests.get(timeout=5)` on a cache miss, and the Tk
        thread takes this same lock to read the ledger for the status label —
        holding it across a network round trip is the UI-stall class this
        project's monitor-glob guard exists to prevent (measured: a 0.4s
        stand-in blocked a `death_count` read for 1.15s). The record is created
        with `str(id)` placeholders inside the lock and STAGED, not published;
        `_resolve_deferred` fills the real names in afterwards and publishes it
        in the same locked write-back. By the time this returns, the record the
        result carries is fully resolved and visible — and it was never visible
        before that (the publication barrier; see the module docstring).
        """
        with self._lock:
            result, deferred = self._ingest_locked(kill_data)
        # NOTHING here holds the lock: this is where the injected callables run.
        if deferred is not None:
            self._resolve_deferred(deferred)
        return result

    def _ingest_locked(self, kill_data: dict) -> tuple[ReconcileResult, "_Deferred | None"]:
        """`ingest`'s body, with the lock already held. Returns the result plus
        any name/tackle work that must NOT run under the lock."""
        if self._source == SOURCE_ESI:
            return ReconcileResult(ACTION_IGNORED, REASON_SOURCE_DISABLED), None

        km = parse_killmail(kill_data)
        if km is None:
            return ReconcileResult(ACTION_IGNORED, REASON_UNPARSEABLE), None
        if not km.character_id:
            # Structure, NPC or citadel loss — no pilot to attribute.
            return ReconcileResult(ACTION_IGNORED, REASON_NO_VICTIM,
                                   killmail=km), None

        # Backfill gate. zKill accepts manually-posted historical killmails
        # and they enter the live stream like fresh ones; the SAME predicate
        # the monitor's own filters use rejects them here.
        inner = _inner_killmail(kill_data) or {}
        if is_kill_stale(inner, self._max_kill_age):
            return ReconcileResult(ACTION_IGNORED, REASON_STALE, killmail=km), None

        if km.time is None:
            # Without a time we can neither window-match nor replay-guard
            # it, and inventing a death from a possibly-ancient killmail is
            # strictly worse than dropping it.
            return ReconcileResult(ACTION_IGNORED, REASON_NO_TIME, killmail=km), None
        if km.time < self._started_at:
            return ReconcileResult(ACTION_IGNORED, REASON_STARTUP_REPLAY,
                                   killmail=km), None

        if km.killmail_id is not None and km.killmail_id in self._seen_killmail_ids:
            return ReconcileResult(ACTION_IGNORED, REASON_DUPLICATE, killmail=km), None

        if km.character_id not in self._roster:
            # Goal 3's whole ownership test. Not on the live roster, not our
            # loss. (Unrecorded on purpose: remembering every killmail id in
            # the firehose would leak memory for no benefit.)
            return ReconcileResult(ACTION_IGNORED, REASON_NOT_OURS, killmail=km), None

        if km.killmail_id is not None:
            self._seen_killmail_ids.add(km.killmail_id)

        if km.is_capsule:
            return self._ingest_pod(km), None
        return self._ingest_ship(km)

    # ── internals ──────────────────────────────────────────────────────────

    def _ingest_pod(self, km: Killmail) -> ReconcileResult:
        """A capsule killmail. SHIP+POD DEDUP lives here: one dead pilot emits
        two killmails, so the pod adds nothing to the count — it only flags the
        ship loss as `podded`, which is precisely the signal that the ESI
        detector was blind for that pilot (a podded pilot respawns docked).

        The pool is every record in either ledger, not `_ledger()`: which loss a
        capsule belongs to cannot depend on the display mode.
        """
        death = self._match_killmail(km, pool=self._all_records(),
                                     match_hull=False, pod_relation=True)
        if death is not None:
            death.podded = True
            return ReconcileResult(ACTION_PODDED, REASON_POD_MARKED, death, km)
        # The pod beat its own ship killmail (or the ship loss never reached
        # zKill). The flag is remembered and applied when the loss shows up —
        # ONCE, to the loss it belongs to (see `_take_pending_pod`).
        self._remember_pending_pod(km)
        return ReconcileResult(ACTION_PODDED, REASON_POD_DEFERRED, None, km)

    def _ingest_ship(self, km: Killmail) -> tuple[ReconcileResult, "_Deferred | None"]:
        """Confirm the inference this killmail describes, or add the loss.

        Mode-INDEPENDENT on purpose. Confirming only in hybrid meant a killmail
        in `zkill` mode added a second record for a loss the ESI ledger already
        held, and a later switch to hybrid then counted both; `_ledger()` is
        where the mode is applied, and it counts a CONFIRMED inference as
        zKill's (it is backed by a real killmail).
        """
        death = self._match_killmail(km, pool=self._unmatched_esi_deaths())
        if death is not None:
            deferred = self._confirm(death, km)
            return ReconcileResult(ACTION_CONFIRMED, REASON_OK, death, km), deferred
        death, deferred = self._add_death(km)
        return ReconcileResult(ACTION_ADDED, REASON_OK, death, km), deferred

    def _unmatched_esi_deaths(self) -> list[DeathEvent]:
        return [d for d in self._esi_deaths if d.killmail_id is None]

    def _in_window(self, killmail_time: datetime, inferred_time: datetime) -> bool:
        """The asymmetric match window, stated once (research §4.2):

            killmail_time - MATCH_LEAD_S <= inferred <= killmail_time + MATCH_WINDOW_S

        Both arguments must already be aware UTC.
        """
        return (killmail_time - self._match_lead) <= inferred_time \
            <= (killmail_time + self._match_window)

    def _in_pod_window(self, pod_time: datetime, ship_stamp: datetime) -> bool:
        """The POD relation's own asymmetry — see POD_MATCH_LEAD_S:

            pod_time - POD_MATCH_LEAD_S <= ship_stamp <= pod_time + POD_MATCH_TRAIL_S

        Generous lead (the ship died before the pod, possibly minutes before),
        tight trail (only the ESI inference lag). This is NOT `_in_window` with
        the arguments swapped: that one is anchored on a ship killmail and leans
        the other way. Both arguments must already be aware UTC.
        """
        return (pod_time - self._pod_lead) <= ship_stamp \
            <= (pod_time + self._pod_trail)

    def _match_killmail(self, km: Killmail, *, pool: list[DeathEvent],
                        match_hull: bool = True,
                        pod_relation: bool = False) -> DeathEvent | None:
        """Find the death in `pool` that this killmail describes, or None.

        Same character, and the death's timestamp inside the window anchored on
        `killmail_time` — `_in_pod_window` for a capsule (a capsule describes
        the ship loss that PRECEDED it), `_in_window` otherwise. When more than
        one death qualifies — the pilot re-shipped and died again inside the
        window — `ship_type_id` joins the key, then nearest-in-time wins.
        """
        if km.time is None:
            return None
        in_window = self._in_pod_window if pod_relation else self._in_window
        candidates: list[tuple[DeathEvent, datetime]] = []
        for death in pool:
            if death.character_id != km.character_id:
                continue
            stamp = to_utc(death.timestamp)
            if stamp is None or not in_window(km.time, stamp):
                continue
            candidates.append((death, stamp))
        if not candidates:
            return None
        if match_hull and len(candidates) > 1 and km.ship_type_id:
            same_hull = [c for c in candidates if c[0].ship_type_id == km.ship_type_id]
            if same_hull:
                candidates = same_hull
        return min(candidates, key=lambda c: abs(c[1] - km.time))[0]

    # ── deferred capsule killmails ─────────────────────────────────────────

    def _remember_pending_pod(self, km: Killmail):
        """Remember a capsule killmail that arrived before its ship loss.

        Opportunistically drops entries whose ship loss can no longer land
        inside the pod window, so a long fleet op cannot accumulate pods that
        will never match. (Lock already held.)
        """
        horizon = km.time - (self._pod_lead + self._pod_trail)
        self._pending_pods = [(cid, when) for cid, when in self._pending_pods
                              if when >= horizon]
        self._pending_pods.append((km.character_id, km.time))

    def _take_pending_pod(self, character_id: int, stamp) -> bool:
        """CONSUME the deferred capsule killmail this ship loss belongs to.

        Consuming is the whole point: `podded` is a property of one loss, not of
        the pilot. A set of "pilots who have been podded" (which is what this
        replaced) flags every later loss by that pilot for the rest of the
        fleet's life, including ESI-inferred ones with no capsule killmail
        anywhere near them. (Lock already held.)
        """
        stamp = to_utc(stamp)
        if stamp is None or not character_id:
            return False
        best: int | None = None
        for index, (cid, pod_time) in enumerate(self._pending_pods):
            if cid != character_id or not self._in_pod_window(pod_time, stamp):
                continue
            if best is None or abs(pod_time - stamp) < \
                    abs(self._pending_pods[best][1] - stamp):
                best = index
        if best is None:
            return False
        del self._pending_pods[best]
        return True

    def _match_zkill_twin(self, death: DeathEvent) -> DeathEvent | None:
        """The mirror of `_match_killmail`: given a freshly INFERRED death, find
        the zKill-sourced record that already describes it.

        The window is anchored on the killmail time in both directions, so here
        the anchor is each candidate's own timestamp (zKill records are stamped
        with `killmail_time`), not the inferred death's.

        The hull check applies to a LONE candidate too. Unlike `_match_killmail`
        — where `_confirm` overwrites the hull from the killmail behind it — a
        wrong match here silently swallows a real second loss (pilot dies in a
        Machariel, re-ships, dies in a Crow) and leaves the surviving record
        self-contradictory: a Machariel killmail wearing the name "Crow".
        """
        stamp = to_utc(death.timestamp)
        if stamp is None:
            return None
        candidates: list[tuple[DeathEvent, datetime]] = []
        # Staged records count: `note_esi_deaths` runs on the Tk thread and can
        # land inside a resolve window, and an inference that failed to see the
        # killmail's record would be appended as a SECOND loss.
        for existing in self._zkill_deaths + self._zkill_pending:
            if existing.character_id != death.character_id:
                continue
            if id(existing) in self._absorbed_zkill:
                continue          # one record absorbs at most one inference
            anchor = to_utc(existing.timestamp)
            if anchor is None or not self._in_window(anchor, stamp):
                continue
            candidates.append((existing, anchor))
        if not candidates:
            return None
        if death.ship_type_id:
            same_hull = [c for c in candidates
                         if c[0].ship_type_id == death.ship_type_id]
            if same_hull:
                candidates = same_hull
            elif any(c[0].ship_type_id for c in candidates):
                # Every candidate names a DIFFERENT hull: none of them is this
                # loss. Two records is the correct answer here.
                return None
        return min(candidates, key=lambda c: abs(c[1] - stamp))[0]

    def _absorb_into_zkill_record(self, zkill_death: DeathEvent,
                                  esi_death: DeathEvent):
        """Fold an inferred death into the zKill record that beat it.

        The killmail already holds better hull/ISK data, so only the names the
        reconciler could not resolve are taken across — without an injected
        resolver they are bare `str(id)` placeholders, while the ESI roster
        carried the real strings. A name that is NOT the placeholder was
        resolved from the killmail itself and is never overwritten.

        The ground truth travels the other way too: the inference is kept as the
        "esi" ledger's shadow of this loss (see `_absorbed_esi_deaths`), so it
        gets the killmail id and ISK. `podded` is a union — either side may have
        learned it first.
        """
        if esi_death.character_name and \
                zkill_death.character_name == str(zkill_death.character_id):
            zkill_death.character_name = esi_death.character_name
        if esi_death.system_name and \
                zkill_death.system_name == str(zkill_death.system_id):
            zkill_death.system_name = esi_death.system_name
        # Only from a record describing the SAME hull: the two agree by the time
        # they get here (`_match_zkill_twin`), and a mismatch would rename the
        # killmail's exact hull after the ESI path's guess.
        if esi_death.ship_name \
                and esi_death.ship_type_id == zkill_death.ship_type_id \
                and zkill_death.ship_name == str(zkill_death.ship_type_id):
            zkill_death.ship_name = esi_death.ship_name
        if esi_death.podded or zkill_death.podded:
            esi_death.podded = zkill_death.podded = True
        esi_death.killmail_id = zkill_death.killmail_id
        esi_death.isk_value = zkill_death.isk_value

    def _confirm(self, death: DeathEvent, km: Killmail) -> "_Deferred | None":
        """Attach ground truth to an ESI-inferred death.

        The hull is replaced because the ESI path only ever had the LAST
        OBSERVED hull (a guess); the killmail has the exact one. `system_id` /
        `system_name` are left alone — the tracker's confirm branch already
        requires the pod to be in the same system as the lost ship, and
        rewriting the id without a name resolver could leave the pair
        disagreeing. `source` stays "esi": the death was inferred there,
        zKill only confirmed it.

        Returns the hull RENAME as deferred work — resolving it here would hold
        the lock across a network call (see `ingest`).
        """
        death.killmail_id = km.killmail_id
        death.isk_value = km.isk_value
        if self._take_pending_pod(km.character_id, km.time):
            death.podded = True
        if not km.ship_type_id:
            return None
        death.ship_type_id = km.ship_type_id
        if self._resolve_name is None:
            # No resolver: `ship_name` keeps the ESI string (today's behaviour).
            return None
        return _Deferred(death=death,
                         lookups=(("ship_name", km.ship_type_id, "type"),),
                         placeholder_only=False,
                         expect_ship=km.ship_type_id)

    def _add_death(self, km: Killmail) -> tuple[DeathEvent, "_Deferred | None"]:
        """Create the death ESI structurally could not see.

        Names start as their `str(id)` placeholders and the returned `_Deferred`
        fills them in once `ingest` has dropped the lock — see `ingest`. Until
        that write-back the record is STAGED rather than published, so no reader
        can observe (and permanently log) a placeholder name.
        """
        death = DeathEvent(
            character_id=km.character_id,
            character_name=str(km.character_id),
            ship_type_id=km.ship_type_id,
            ship_name=str(km.ship_type_id),
            system_id=km.system_id,
            system_name=str(km.system_id),
            # The killmail time is the TRUE death instant, not the moment we
            # heard about it ~2 minutes later.
            timestamp=km.time,
            killmail_id=km.killmail_id,
            isk_value=km.isk_value,
            podded=self._take_pending_pod(km.character_id, km.time),
            source=DEATH_SOURCE_ZKILL,
        )
        # `_was_tackle` is a setattr, not a field (QUALITY_AUDIT C2-16); mirror
        # the tracker's convention so consumers' getattr() reads work either way.
        # It starts False and the deferred pass reclassifies — `is_tackle` can
        # trigger an ESI type->group lookup for an uncached hull.
        setattr(death, "_was_tackle", False)
        if self._resolve_name is None and self._is_tackle is None:
            # Nothing injected: nothing to defer, so the record is already
            # final and is published straight away (today's wiring).
            self._zkill_deaths.append(death)
            return death, None
        self._zkill_pending.append(death)
        return death, _Deferred(
            death=death,
            lookups=(("character_name", km.character_id, "character"),
                     ("ship_name", km.ship_type_id, "type"),
                     ("system_name", km.system_id, "solar_system")),
            placeholder_only=True,
            expect_ship=km.ship_type_id,
            tackle_type_id=km.ship_type_id if self._is_tackle is not None else 0,
        )

    def _resolve_deferred(self, work: "_Deferred"):
        """Run the injected callables with NO lock held, then write back AND
        publish, in one locked step.

        The write-back re-acquires briefly and is defensive about what may have
        happened in between: another killmail may have absorbed a real name into
        this record, or re-confirmed the death onto a different hull.

        The lock is taken even when nothing resolved — an early return would
        strand a staged record in `_zkill_pending` forever, i.e. lose the loss.
        """
        resolved: list[tuple[str, int, str]] = []
        for attribute, entity_id, category in work.lookups:
            name = self._name(entity_id, category)
            if name:
                resolved.append((attribute, entity_id, name))
        tackle = self._tackle(work.tackle_type_id) if work.tackle_type_id else None
        death = work.death
        with self._lock:
            for attribute, entity_id, name in resolved:
                if attribute == "ship_name" and work.expect_ship \
                        and death.ship_type_id != work.expect_ship:
                    continue        # re-confirmed onto another hull meanwhile
                if work.placeholder_only \
                        and getattr(death, attribute, None) != str(entity_id):
                    continue        # a real name landed while we were on the wire
                setattr(death, attribute, name)
            if tackle is not None:
                setattr(death, "_was_tackle", tackle)
            self._publish_locked(death)

    def _publish_locked(self, death: DeathEvent):
        """Move a now-final zKill record out of staging into the public ledger.

        Identity, not equality: two losses by the same pilot in the same hull
        are distinct records. A death that was never staged (the CONFIRM path,
        or one dropped by a `reset` mid-resolve) is simply not found and this
        is a no-op — which is the right answer in both cases.

        Publication order is resolve-completion order and each record is
        appended exactly once, so `_zkill_deaths` only ever grows at its END.
        `fc_gui._log_zkill_only_losses` depends on that: its watermark logs the
        list by index, and a record inserted behind the mark would be skipped
        while a later one was logged twice. (Lock already held.)
        """
        for index, staged in enumerate(self._zkill_pending):
            if staged is death:
                del self._zkill_pending[index]
                self._zkill_deaths.append(death)
                return

    def _name(self, entity_id: int, category: str) -> str | None:
        """Resolve a name via the injected resolver, or None when there is no
        resolver (callers substitute str(id)). Never raises and never makes a
        request of its own.

        A resolver that answers with a non-string (or "") is treated as a
        failure: `str(id)` is a usable label, `None` printed into the loss log
        is not. CALL THIS WITH NO LOCK HELD — the intended injection
        (`zkill_monitor.resolve_name`) does a rate-limited HTTP GET on a miss.
        """
        if self._resolve_name is None or not entity_id:
            return None
        try:
            resolved = self._resolve_name(entity_id, category)
        except Exception:
            return None
        return resolved if isinstance(resolved, str) and resolved else None

    def _tackle(self, ship_type_id: int) -> bool:
        """Classify a hull via the injected predicate. Never raises; defaults to
        False (= a major loss). CALL THIS WITH NO LOCK HELD — `ship_classes.
        is_tackle` can trigger an ESI type->group lookup for an uncached hull."""
        if self._is_tackle is None or not ship_type_id:
            return False
        try:
            return bool(self._is_tackle(ship_type_id))
        except Exception:
            return False

    def _all_records(self) -> list[DeathEvent]:
        """Every record a MATCHER may consider, whatever the mode — including
        the ones still resolving, so a capsule killmail arriving inside a
        resolve window still flags the ship loss it belongs to instead of being
        deferred against a loss that is already here. (Lock already held.)"""
        return self._esi_deaths + self._zkill_deaths + self._zkill_pending

    def _ledger(self) -> list[DeathEvent]:
        """The reconciled death list for the active mode (lock already held).

        This is the ONLY place the mode is COMPOSED (the one other read of
        `self._source` is `_ingest_locked`'s all-or-nothing stop in "esi"
        mode). The ingest path stores one record per loss and never partially
        branches on the mode, so composing the ledger from PROVENANCE is what
        keeps a live source switch from doubling or dropping anything.

        Staged records are deliberately absent: an unpublished record is not
        yet part of any ledger (see `_publish_locked`).
        """
        if self._source == SOURCE_ESI:
            # Everything the capsule detector inferred — including the deaths
            # whose killmail arrived first and absorbed them.
            return self._esi_deaths + self._absorbed_esi_deaths
        if self._source == SOURCE_ZKILL:
            # Everything zKillboard reported: its own records, plus the
            # inferences a killmail CONFIRMED (the raw ESI ledger is not
            # counted, but a confirmed death is a killmail's).
            return [d for d in self._esi_deaths if d.killmail_id is not None] \
                + self._zkill_deaths
        return self._esi_deaths + self._zkill_deaths

    # ── results ────────────────────────────────────────────────────────────

    @property
    def deaths(self) -> list[DeathEvent]:
        """Reconciled deaths for the active mode, oldest first.

        esi    -> ESI-inferred only (including any a killmail absorbed).
        zkill  -> killmail-backed only: zKill-sourced records plus confirmed
                  inferences (the raw ESI ledger is not counted, so nothing
                  doubles).
        hybrid -> ESI-inferred, enriched in place, PLUS the zKill-only losses.
        """
        with self._lock:
            ledger = self._ledger()
        return sorted(ledger, key=_sort_key)

    @property
    def death_count(self) -> int:
        with self._lock:
            return len(self._ledger())

    @property
    def zkill_only_deaths(self) -> list[DeathEvent]:
        """Deaths ADDED from killmails — the losses ESI was blind to.

        Published records only, oldest publication first. A record whose names
        are still being resolved is not here yet; it appears, fully named, as
        soon as `ingest` returns.
        """
        with self._lock:
            return list(self._zkill_deaths)

    @property
    def zkill_only_count(self) -> int:
        with self._lock:
            return len(self._zkill_deaths)

    @property
    def confirmed_count(self) -> int:
        """Ledger deaths backed by a real killmail."""
        with self._lock:
            return sum(1 for d in self._ledger() if d.killmail_id is not None)

    @property
    def podded_count(self) -> int:
        with self._lock:
            return sum(1 for d in self._ledger() if d.podded)

    @property
    def total_isk_lost(self) -> float:
        """Raw ISK across the ledger. Ship killmails only — see the module
        docstring on why capsule value is excluded."""
        with self._lock:
            return float(sum(d.isk_value for d in self._ledger()))


# ── Config adapter (app config -> engine) ───────────────────────────────────
# FCTool NEVER deep-merges config.json against DEFAULT_CONFIG — `_load_config`
# takes the file verbatim or falls back to the defaults wholesale — so an
# install that predates this feature simply HAS NO `loss_tracking` block, and a
# hand-edited one may hold anything at all. Reading the block defensively IS the
# entire migration; there is no migration code anywhere else.
#
# It lives in this module rather than in fc_gui so the defaulting is testable
# offline and the wiring layer keeps only a one-line hookup (fc_gui containment).

CONFIG_KEY = "loss_tracking"


def _config_int(value, fallback: int) -> int:
    """Coerce a config number. Anything non-numeric (None, "", a typo, a dict)
    or negative falls back rather than raising."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return fallback
    return number if number >= 0 else fallback


def config_block(config) -> dict:
    """The `loss_tracking` block, or {} when it is absent or not a dict."""
    block = config.get(CONFIG_KEY) if isinstance(config, dict) else None
    return block if isinstance(block, dict) else {}


def source_from_config(config) -> str:
    """The selected mode, normalized. An absent block, an absent key and a
    garbage value all give DEFAULT_SOURCE — a hand-edited config must never
    break loss tracking."""
    return normalize_source(config_block(config).get("source"))


def status_summary(rec: LossReconciler, isk_format=str, *,
                   zkill_headline: bool = False) -> str:
    """Compact reconciled-loss segment for a status line, or "".

    Pure presentation over reconciler state, kept here so the wiring layer holds
    no formatting. `isk_format` renders a raw ISK float — FCToolGUI passes its
    existing compact formatter, so the app keeps exactly one ISK formatter.

    Returns "" in "esi" mode and until zKill has actually said something, so a
    caller may append it unconditionally and a quiet label stays byte-identical
    to what it showed before this feature existed.

    `zkill_headline` says the caller's own headline ALREADY displays this
    ledger under a zKill label ("Losses: 2 [zKill]"). Two things follow, and
    "zkill" mode implies both on its own:

      * the "+N unseen" count is dropped — it means "losses the ESI capsule
        detector could not see", which is only a fact worth stating next to an
        ESI-derived count. Where the reconciler's ledger IS the displayed
        number, every loss in it is "unseen" and the segment would just
        restate the headline (or, off-boss, name a detector that never ran).
      * the "zKill " prefix is dropped — "Losses: 2 [zKill]  [zKill 2.0B ISK]"
        said it twice.
    """
    if rec.source == SOURCE_ESI:
        return ""
    ledger_is_the_headline = zkill_headline or rec.source == SOURCE_ZKILL
    bits = []
    if not ledger_is_the_headline:
        unseen = rec.zkill_only_count
        if unseen:
            bits.append(f"+{unseen} unseen")  # losses ESI structurally can't see
    isk = rec.total_isk_lost
    if isk > 0:
        bits.append(f"{isk_format(isk)} ISK")
    if not bits:
        return ""
    prefix = "" if ledger_is_the_headline else "zKill "
    return f"  [{prefix}{', '.join(bits)}]"


def from_config(config, **overrides) -> LossReconciler:
    """Build a `LossReconciler` from the app config.

    `overrides` win over the config, so a caller can still inject the optional
    `is_tackle` / `resolve_name` capabilities or a fixed `started_at`.
    """
    block = config_block(config)
    kwargs = dict(
        source=source_from_config(config),
        match_window_s=_config_int(block.get("match_window_s"), MATCH_WINDOW_S),
        match_lead_s=_config_int(block.get("match_lead_s"), MATCH_LEAD_S),
    )
    kwargs.update(overrides)
    return LossReconciler(**kwargs)
