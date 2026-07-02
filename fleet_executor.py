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
from collections import deque

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

MOVE_SOURCES = ("drag", "menu", "apply", "autosort")


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
                 remaining_needed=lambda job: True, now=None, autostart=True):
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
        self._now = now or _time.monotonic
        self._q: _queue.Queue = _queue.Queue()
        self._worker: _threading.Thread | None = None
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
        self._q.put(job)

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
            self._process_one(job)

    # ── worker ───────────────────────────────────────────────────────────────
    def _run(self):
        """Persistent worker: parks on a BLOCKING get; exits only on the sentinel."""
        while True:
            job = self._q.get()          # blocks until a job (or stop() sentinel)
            if job is self._STOP:
                return
            self._process_one(job)
            if self._q.empty():
                self._on_idle()

    def _on_idle(self):
        """Queue drained — reset continuous-run pacing state for the next burst."""
        self._wrote_any = False
        self._burst = 0

    def _process_one(self, job: MoveJob):
        """One iteration of the shared loop body (pacing + write + burst/settle).

        Task 4 adds the gate + abort/freeze branches around this."""
        if self._wrote_any:
            self._sleep(self._spacing_s)   # spacing BETWEEN consecutive writes only
        self._log(self._perform(job))
        self._wrote_any = True
        self._burst += 1
        if self._burst >= self._burst_cap and not self._q.empty():
            self._settle_and_repoll()

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
                self._q.put(job)

    def _perform(self, job: MoveJob):
        """Execute one write, spend tokens, reconcile from headers. Returns a
        (job, status, error) tuple for logging. Error paths land in Task 4."""
        status = self._on_move(job)
        self.ledger.spend(cost_for_status(status))
        self.ledger.reconcile(self._header_remaining())
        return (job, status, None)

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
