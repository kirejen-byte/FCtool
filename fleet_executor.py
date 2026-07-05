# fleet_executor.py
"""Serialized fleet-write engine — token ledger + paced executor.

Pure/injectable by design: `FleetExecutor` takes a session, a monotonic clock,
a `post` callable (marshals callbacks to the Tk thread) and a `sleep` fn, so its
whole state machine is testable with fakes — no Tk, no network, no real sleeps.

Ledger (spec §"ESI facts"): the fleet route group shares 1800 tokens / 15 min
across ALL fleet reads+writes. Costs: 2XX=2, 304=1, 4XX=5 (except 429), 5XX=0.
X-Ratelimit-Remaining is authoritative when present (reconcile()).
"""
from __future__ import annotations

import time as _time
from collections import Counter, deque

DEFAULT_BUDGET = 1800
DEFAULT_WINDOW_S = 900


def cost_for_status(status: int) -> int:
    """Token cost of an ESI response by status class (spec §ESI facts).

    429 is special-cased by the executor (it freezes rather than spending a
    4xx cost), so this table returns the plain-4xx cost for it too — callers
    that hit a 429 branch before spending never reach here for that status."""
    if 200 <= status < 300:
        return 2
    if status == 304:
        return 1
    if 400 <= status < 500:
        return 5
    return 0   # 5xx (and anything else non-4xx/2xx/304)


class FleetTokenLedger:
    """Rolling window of (monotonic, tokens) spends; estimates remaining budget."""

    def __init__(self, budget: int = DEFAULT_BUDGET,
                 window_s: int = DEFAULT_WINDOW_S, *, now=None):
        self._budget = budget
        self._window = window_s
        self._now = now or _time.monotonic
        self._spends: deque = deque()   # (timestamp, tokens)
        self._override: int | None = None       # last header-reported remaining
        self._override_spent = 0                 # tokens spent since that override

    def _evict(self):
        cutoff = self._now() - self._window
        while self._spends and self._spends[0][0] < cutoff:
            self._spends.popleft()

    def spend(self, cost: int) -> None:
        self._spends.append((self._now(), cost))
        if self._override is not None:
            self._override_spent += cost

    def reconcile(self, header_remaining) -> None:
        """Trust X-Ratelimit-Remaining over local math when it's an int."""
        if isinstance(header_remaining, bool) or not isinstance(header_remaining, int):
            return
        self._override = header_remaining
        self._override_spent = 0

    def remaining(self) -> int:
        if self._override is not None:
            return self._override - self._override_spent
        self._evict()
        return self._budget - sum(t for _, t in self._spends)


# append to fleet_executor.py
import queue as _queue
import threading as _threading
from dataclasses import dataclass

import fleet_esi

MOVE_SOURCES = ("drag", "menu", "apply", "autosort")

AUTOSORT_FLOOR = 360
HARD_FLOOR = 180
CONSECUTIVE_4XX_ABORT = 3


@dataclass
class MoveJob:
    pilot_id: int
    pilot_name: str
    wing_id: int | None
    squad_id: int | None
    role: str
    source: str   # one of MOVE_SOURCES


class FleetExecutor:
    """One daemon worker draining a queue of MoveJobs with ESI-safe pacing.

    Injected collaborators keep it pure-testable:
      on_move(job) -> int   perform the ESI write, return the HTTP status.
      post(fn,*a)           marshal a callback to the Tk thread.
      sleep(s)              pacing sleep (fake in tests).
      on_log(line)          append a timestamped-ish line to the window log.
      on_repoll()           trigger a members re-poll after a burst settle.
      remaining_needed(job) after a re-poll, is this queued job still needed?
      ledger                FleetTokenLedger (shared with sync reads).
    """

    _STOP = object()   # sentinel pushed by stop() to end the persistent worker

    def __init__(self, *, session, on_move, post, sleep, ledger,
                 move_spacing_ms=400, burst_cap=25, settle_s=3,
                 on_log=lambda line: None, on_repoll=None,
                 remaining_needed=lambda job: True, on_boss_lost=lambda: None,
                 now=None, autostart=True):
        self.session = session
        self._on_move = on_move
        self._post = post
        self._sleep = sleep
        self.ledger = ledger
        self._spacing_s = move_spacing_ms / 1000.0
        self._burst_cap = burst_cap
        self._settle_s = settle_s
        self._on_log = on_log
        self._on_repoll = on_repoll
        self._remaining_needed = remaining_needed
        self._on_boss_lost = on_boss_lost
        self._consecutive_4xx = 0
        self._aborted = False
        self._now = now or _time.monotonic
        self._q: _queue.Queue = _queue.Queue()
        self._worker: _threading.Thread | None = None
        # Pending-pilot ledger (C3-04): pilot_id -> count of queued-or-in-flight
        # MoveJobs. A Counter (multiset), not a set, so a manual + auto-sort job
        # for the same pilot don't clear each other prematurely. Incremented in
        # submit(); decremented on EVERY disposal path (complete / gate-drop /
        # abort-drain / freeze-drain / repoll-drop). `submit` runs on the Tk
        # thread while disposal runs on the worker thread, so the counter is
        # guarded by _pending_lock. Queried by has_pending()/pending_pilot_ids()
        # so the auto-sort tick can skip pilots that already have a job in flight.
        self._pending: Counter = Counter()
        self._pending_lock = _threading.Lock()
        # Persistent run-state — survives across submits so sequential writes are
        # paced as one continuous run. Reset only when the queue goes idle.
        self._wrote_any = False
        self._burst = 0
        self._frozen_until = 0.0     # used by Task 4
        if autostart:
            self.start()

    # ── public API ───────────────────────────────────────────────────────────
    def start(self) -> None:
        """Launch the single persistent worker (idempotent)."""
        if self._worker is not None and self._worker.is_alive():
            return
        self._worker = _threading.Thread(target=self._run, daemon=True)
        self._worker.start()

    def stop(self) -> None:
        """Signal the persistent worker to exit (pushes the None/_STOP sentinel)."""
        self._q.put(self._STOP)

    def submit(self, job: MoveJob) -> None:
        # Self-healing: if the persistent worker was ever started but its thread
        # has since died (should not happen now that the loop is guarded, but a
        # last line of defence against stranded queues), restart it before
        # enqueueing. start() no-ops when the worker is already alive, so this is
        # a cheap check on the normal path and a no-op for drain()-only executors
        # that never started a worker (self._worker is None).
        if self._worker is not None and not self._worker.is_alive():
            self.start()
        # Track the pilot BEFORE enqueueing so a query racing the enqueue never
        # sees the job land untracked. Exactly one increment per submitted job;
        # _process_one's finally-clause / drain paths decrement it exactly once.
        self._track(job.pilot_id)
        self._q.put(job)

    def _track(self, pilot_id) -> None:
        with self._pending_lock:
            self._pending[pilot_id] += 1

    def _untrack(self, pilot_id) -> None:
        """Drop one pending reference for a pilot. Idempotent-safe at zero (a
        stray/duplicate untrack for an already-cleared pilot is a no-op rather
        than driving the count negative)."""
        with self._pending_lock:
            n = self._pending.get(pilot_id, 0)
            if n <= 1:
                self._pending.pop(pilot_id, None)
            else:
                self._pending[pilot_id] = n - 1

    def has_pending(self, pilot_id) -> bool:
        """True if this pilot has a queued-or-in-flight MoveJob (C3-04 dedup)."""
        with self._pending_lock:
            return pilot_id in self._pending

    def pending_pilot_ids(self) -> set:
        """Snapshot of all pilot ids with a queued-or-in-flight MoveJob."""
        with self._pending_lock:
            return set(self._pending)

    def drain(self) -> None:
        """Run the exact worker loop body inline until the queue is empty.

        Deterministic test/reconcile driver — no real thread, no Event waits.
        Shares `_process_one` with the production worker so it exercises the
        real pacing/burst/settle code path."""
        while True:
            try:
                job = self._q.get_nowait()
            except _queue.Empty:
                self._on_idle()
                return
            if job is self._STOP:
                self._on_idle()
                return
            try:
                self._process_one(job)
            except Exception as e:
                # Belt-and-braces: an unexpected error must never abort the drain
                # loop. _perform already downgrades on_move errors to non-fatal
                # results; this catches anything else in the pipeline.
                self._post(self._on_log,
                           f"executor error on {job.pilot_name}: {e!r}")

    # ── worker ───────────────────────────────────────────────────────────────
    def _run(self):
        """Persistent worker: parks on a BLOCKING get; exits only on the sentinel."""
        while True:
            job = self._q.get()          # blocks until a job (or stop() sentinel)
            if job is self._STOP:
                return
            try:
                self._process_one(job)
            except Exception as e:
                # Belt-and-braces: an unexpected error must never kill the
                # persistent worker thread (which would strand every later job in
                # the queue forever). Log it and keep serving the next job.
                self._post(self._on_log,
                           f"executor error on {job.pilot_name}: {e!r}")
            if self._q.empty():
                self._on_idle()

    def _on_idle(self):
        """Queue drained — reset continuous-run pacing state for the next burst."""
        self._wrote_any = False
        self._burst = 0

    def _process_one(self, job: MoveJob):
        # The current job leaves the pipeline down EXACTLY one of these branches
        # (completed, gate-dropped, or discarded because the queue is
        # aborted/frozen). The finally untracks its pilot once, regardless of
        # which branch (or an unexpected raise) exits — so C3-04's pending ledger
        # never leaks a reference. _drain_remaining / _settle_and_repoll untrack
        # the OTHER jobs they discard; they never touch this job (already popped).
        try:
            if self._aborted:
                self._drain_remaining()
                return
            if not self._gate(job):
                return   # gated jobs are skipped (logged), no spacing consumed
            if self._wrote_any:
                self._sleep(self._spacing_s)   # spacing BETWEEN consecutive writes only
            self._log(self._perform(job))
            self._wrote_any = True
            if self._aborted:               # boss_lost / 3rd-4xx set this in _perform
                self._drain_remaining()
                return
            if self._frozen_until:          # 429/420 froze ops mid-run
                self._drain_remaining()
                return
            self._burst += 1
            if self._burst >= self._burst_cap and not self._q.empty():
                self._settle_and_repoll()
        finally:
            self._untrack(job.pilot_id)

    def _drain_remaining(self):
        """Discard every remaining queued job (abort/freeze). A pending stop
        sentinel is preserved so stop() still ends the persistent worker.
        Each discarded MoveJob is untracked from the pending ledger (C3-04)."""
        while True:
            try:
                item = self._q.get_nowait()
            except _queue.Empty:
                return
            if item is self._STOP:
                self._q.put(item)
                return
            self._untrack(item.pilot_id)

    def _gate(self, job: MoveJob) -> bool:
        if self._frozen_until:
            remaining = self._frozen_until - self._now()
            if remaining > 0:
                # Server asked us to back off (429/420). Drop NEW work for the
                # whole freeze window — auto-sort re-submits on a later tick, so
                # dropping is safe and matches the mid-run freeze-drain semantics.
                self._post(self._on_log,
                           f"[{job.source}] {job.pilot_name}: frozen "
                           f"{remaining:.0f}s — dropping job")
                return False
            self._frozen_until = 0.0   # freeze elapsed → resume normal operation
        rem = self.ledger.remaining()
        manual = job.source in ("drag", "menu", "apply")
        if rem < HARD_FLOOR:
            if manual:
                self._post(self._on_log,
                           f"[{job.source}] {job.pilot_name}: LOW BUDGET "
                           f"({rem}) — running anyway")
                return True
            self._post(self._on_log,
                       f"[{job.source}] {job.pilot_name}: blocked, budget {rem} < {HARD_FLOOR}")
            return False
        if job.source == "autosort" and rem < AUTOSORT_FLOOR:
            self._post(self._on_log,
                       f"[autosort] {job.pilot_name}: skipped, budget {rem} < {AUTOSORT_FLOOR}")
            return False
        return True

    def _settle_and_repoll(self):
        self._sleep(self._settle_s)
        self._burst = 0
        if self._on_repoll is not None:
            self._on_repoll()
        # Re-verify every still-queued job; drop ones no longer needed.
        kept = []
        while True:
            try:
                item = self._q.get_nowait()
            except _queue.Empty:
                break
            if item is self._STOP:       # preserve a pending stop sentinel
                self._q.put(item)
                break
            kept.append(item)
        for job in kept:
            if self._remaining_needed(job):
                self._q.put(job)   # still queued → stays tracked
            else:
                self._untrack(job.pilot_id)   # dropped → release its reference

    def _perform(self, job: MoveJob):
        try:
            status = self._on_move(job)
        except fleet_esi.FleetESIError as e:
            if e.reason == "boss_lost":
                self._aborted = True
                self._post(self._on_boss_lost)
                return (job, 403, "boss lost (403) — queue aborted")
            self._consecutive_4xx = 0
            return (job, e.status or 0, e.reason)
        except Exception as e:
            # Any unexpected error in on_move (e.g. a bug in the ESI writer) is a
            # NON-FATAL per-job failure: no ledger spend, do not touch the
            # consecutive-4xx counter, do not abort the queue. This keeps one bad
            # job from taking down the whole run (and, in the worker, the thread).
            return (job, 0, f"unexpected error: {e!r}")
        # Non-exception statuses.
        if status == 429:
            retry = self._retry_after()
            self._sleep(retry)
            self._frozen_until = self._now() + retry
            return (job, 429, f"429 rate-limited — froze fleet ops {retry:.0f}s")
        if status == 420:
            reset = self._error_reset()
            self._frozen_until = self._now() + reset
            return (job, 420, f"420 error-limited — froze ESI {reset:.0f}s")
        self.ledger.spend(cost_for_status(status))
        self.ledger.reconcile(self._header_remaining())
        if 400 <= status < 500:
            self._consecutive_4xx += 1
            if self._consecutive_4xx >= CONSECUTIVE_4XX_ABORT:
                self._aborted = True
                return (job, status, f"{status} — 3rd consecutive 4xx, aborting queue")
            return (job, status, f"{status} error")
        self._consecutive_4xx = 0
        return (job, status, None)

    def _retry_after(self) -> float:
        h = getattr(self.session, "last_headers", {}) or {}
        try:
            return float(h.get("Retry-After"))
        except (TypeError, ValueError):
            return 60.0

    def _error_reset(self) -> float:
        h = getattr(self.session, "last_headers", {}) or {}
        try:
            return float(h.get("X-ESI-Error-Limit-Reset"))
        except (TypeError, ValueError):
            return 60.0

    def _header_remaining(self):
        h = getattr(self.session, "last_headers", {}) or {}
        val = h.get("X-Ratelimit-Remaining")
        try:
            return int(val)
        except (TypeError, ValueError):
            return None

    def _log(self, result):
        job, status, error = result
        if error is not None:
            self._post(self._on_log, f"[{job.source}] {job.pilot_name}: {error}")
        else:
            self._post(self._on_log,
                       f"[{job.source}] moved {job.pilot_name} → wing "
                       f"{job.wing_id}/squad {job.squad_id} ({job.role})")
