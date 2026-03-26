"""
API Rate Limiter
Centralized rate limiting for all external API calls.
Ensures compliance with ESI, zKillboard, and EVE Scout rate limits.
"""

import time
import threading
from collections import defaultdict


class RateLimiter:
    """
    Token-bucket rate limiter.
    Tracks calls per endpoint and enforces delays to stay within limits.
    Sleeps outside the lock so multiple endpoints can proceed concurrently.
    """

    # Conservative limits (well under actual limits to be safe)
    LIMITS = {
        "esi": {"calls_per_second": 15, "burst": 30},       # ESI allows ~20/s, we use 15
        "zkill_api": {"calls_per_second": 1, "burst": 2},   # zKill REST is strict
        "evescout": {"calls_per_second": 1, "burst": 3},    # EVE Scout: be polite
        "discord": {"calls_per_second": 2, "burst": 5},     # Discord webhook ~5/s
    }

    def __init__(self):
        self._lock = threading.Lock()
        self._call_times: dict[str, list[float]] = defaultdict(list)

    def wait(self, endpoint: str = "esi"):
        """Block until it's safe to make a call to the given endpoint."""
        limits = self.LIMITS.get(endpoint, {"calls_per_second": 5, "burst": 10})
        max_per_sec = limits["calls_per_second"]
        window = 1.0

        while True:
            with self._lock:
                now = time.monotonic()
                # Prune old entries
                self._call_times[endpoint] = [
                    t for t in self._call_times[endpoint] if now - t < window
                ]

                if len(self._call_times[endpoint]) < max_per_sec:
                    # Slot available — claim it and return
                    self._call_times[endpoint].append(now)
                    return

                # Calculate wait time, but sleep OUTSIDE the lock
                oldest = self._call_times[endpoint][0]
                wait_time = window - (now - oldest) + 0.01

            # Sleep outside the lock so other threads/endpoints aren't blocked
            time.sleep(max(0.01, wait_time))

    def get_stats(self) -> dict[str, int]:
        """Get current call counts per endpoint (in last second)."""
        now = time.monotonic()
        stats = {}
        for ep, times in self._call_times.items():
            recent = [t for t in times if now - t < 1.0]
            stats[ep] = len(recent)
        return stats


# Global singleton
_limiter = RateLimiter()


def rate_limit(endpoint: str = "esi"):
    """Call before any API request to enforce rate limits."""
    _limiter.wait(endpoint)
