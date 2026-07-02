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
